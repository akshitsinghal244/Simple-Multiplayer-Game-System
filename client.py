"""
UDP Multiplayer Game Client
- Client-side prediction: moves instantly on input
- Server reconciliation: corrects mispredictions
- ACK system for reliable critical packets
- pygame renderer

Usage:
    python client.py [name] [server_host] [server_port]
    python client.py Alice localhost 9999
"""

import socket
import json
import time
import threading
import sys
import math
import pygame

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9999
PLAYER_SPEED = 200.0
WORLD_W = 800
WORLD_H = 600
INPUT_RATE = 60         # inputs per second
INTERP_DELAY = 0.1      # seconds of interpolation buffer for remote players

# ─── PACKET TYPES ────────────────────────────────────────────────────────────
PKT_CONNECT    = "CONNECT"
PKT_DISCONNECT = "DISCONNECT"
PKT_ACK        = "ACK"
PKT_JOIN_OK    = "JOIN_OK"
PKT_GAME_STATE = "GAME_STATE"
PKT_INPUT      = "INPUT"
PKT_PLAYER_JOIN = "PLAYER_JOIN"
PKT_PLAYER_QUIT = "PLAYER_QUIT"

# ─── COLORS ──────────────────────────────────────────────────────────────────
BG_COLOR        = (15, 15, 25)
GRID_COLOR      = (30, 30, 50)
GROUND_COLOR    = (25, 25, 40)
UI_BG           = (10, 10, 20, 200)
WHITE           = (255, 255, 255)
GRAY            = (120, 120, 140)
SHADOW          = (0, 0, 0, 80)

def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

# ─── NETWORK CLIENT ──────────────────────────────────────────────────────────

class NetworkClient:
    def __init__(self, host, port, name):
        self.host = host
        self.port = port
        self.name = name
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.01)

        self.connected = False
        self.my_pid = None
        self.lock = threading.Lock()

        # Authoritative state for all players
        self.server_players = {}   # pid -> {x, y, color, name, last_input_seq}

        # Client prediction
        self.local_x = 0.0
        self.local_y = 0.0
        self.input_history = []    # list of {seq, dx, dy, dt, time}
        self.input_seq = 0

        # Interpolation buffer for remote players
        # pid -> list of {t, x, y}
        self.interp_buffer = {}

        # Reliable ACK tracking
        self.pending_acks = {}     # seq -> {packet, retries, last_sent}
        self.out_seq = 0

        # World size from server
        self.world_w = WORLD_W
        self.world_h = WORLD_H

        self.ping_ms = 0
        self._ping_send_time = 0

        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._ack_retry_loop, daemon=True).start()

    # ── SEND HELPERS ─────────────────────────────────────────────────────────

    def _encode(self, data):
        return json.dumps(data).encode()

    def _send(self, data):
        try:
            self.sock.sendto(self._encode(data), self.addr)
        except:
            pass

    def _next_seq(self):
        self.out_seq += 1
        return self.out_seq

    def _send_reliable(self, data):
        seq = data["seq"]
        self.pending_acks[seq] = {
            "packet": data,
            "retries": 0,
            "last_sent": time.time()
        }
        self._send(data)

    def _ack(self, seq):
        self._send({"type": PKT_ACK, "seq": seq})

    # ── CONNECT ──────────────────────────────────────────────────────────────

    def connect(self):
        self._send({"type": PKT_CONNECT, "name": self.name})

    def disconnect(self):
        self._send({"type": PKT_DISCONNECT})

    # ── INPUT SENDING ────────────────────────────────────────────────────────

    def send_input(self, dx, dy, dt):
        if not self.connected:
            return
        self.input_seq += 1
        seq = self.input_seq
        packet = {
            "type": PKT_INPUT,
            "seq": seq,
            "dx": dx,
            "dy": dy,
            "dt": dt,
            "t": time.time()
        }
        # Store for reconciliation
        self.input_history.append({
            "seq": seq,
            "dx": dx,
            "dy": dy,
            "dt": dt,
            "x_before": self.local_x,
            "y_before": self.local_y
        })
        # Keep history bounded
        if len(self.input_history) > 120:
            self.input_history.pop(0)

        self._send(packet)  # Input is unreliable (high frequency)

    # ── PREDICTION ───────────────────────────────────────────────────────────

    def predict_move(self, dx, dy, dt):
        """Apply movement locally immediately (client-side prediction)."""
        if dx != 0 or dy != 0:
            length = math.sqrt(dx*dx + dy*dy)
            dx /= length
            dy /= length
        self.local_x = max(20, min(self.world_w - 20, self.local_x + dx * PLAYER_SPEED * dt))
        self.local_y = max(20, min(self.world_h - 20, self.local_y + dy * PLAYER_SPEED * dt))

    def reconcile(self, server_x, server_y, last_acked_seq):
        """
        Server reconciliation:
        1. Accept server position as ground truth
        2. Re-apply all unacknowledged inputs on top
        """
        # Remove acknowledged inputs
        self.input_history = [i for i in self.input_history if i["seq"] > last_acked_seq]

        # Start from server state
        rx, ry = server_x, server_y

        # Re-simulate unacknowledged inputs
        for inp in self.input_history:
            dx, dy = inp["dx"], inp["dy"]
            dt = inp["dt"]
            if dx != 0 or dy != 0:
                length = math.sqrt(dx*dx + dy*dy)
                dx /= length
                dy /= length
            rx = max(20, min(self.world_w - 20, rx + dx * PLAYER_SPEED * dt))
            ry = max(20, min(self.world_h - 20, ry + dy * PLAYER_SPEED * dt))

        self.local_x = rx
        self.local_y = ry

    # ── INTERPOLATION ────────────────────────────────────────────────────────

    def get_interpolated_pos(self, pid, now):
        """Return interpolated position for a remote player."""
        buf = self.interp_buffer.get(pid, [])
        render_t = now - INTERP_DELAY

        if not buf:
            p = self.server_players.get(pid)
            if p:
                return p["x"], p["y"]
            return 0, 0

        # Find two snapshots to interpolate between
        before = after = None
        for snap in buf:
            if snap["t"] <= render_t:
                before = snap
            elif after is None and snap["t"] > render_t:
                after = snap

        if before is None:
            return buf[0]["x"], buf[0]["y"]
        if after is None:
            return before["x"], before["y"]

        # Linear interpolation
        span = after["t"] - before["t"]
        if span <= 0:
            return before["x"], before["y"]
        alpha = (render_t - before["t"]) / span
        alpha = max(0.0, min(1.0, alpha))
        ix = before["x"] + (after["x"] - before["x"]) * alpha
        iy = before["y"] + (after["y"] - before["y"]) * alpha
        return ix, iy

    # ── RECEIVE LOOP ─────────────────────────────────────────────────────────

    def _recv_loop(self):
        while True:
            try:
                raw, _ = self.sock.recvfrom(8192)
                data = json.loads(raw.decode())
                self._handle(data)
            except socket.timeout:
                pass
            except Exception as e:
                pass

    def _handle(self, data):
        ptype = data.get("type")

        if ptype == PKT_JOIN_OK:
            self._ack(data["seq"])
            with self.lock:
                self.my_pid = data["pid"]
                self.local_x = float(data["x"])
                self.local_y = float(data["y"])
                self.world_w = data.get("world_w", WORLD_W)
                self.world_h = data.get("world_h", WORLD_H)
                # Load existing players
                for pid_str, p in data.get("existing_players", {}).items():
                    pid = int(pid_str)
                    self.server_players[pid] = p.copy()
                    self.interp_buffer[pid] = []
                self.connected = True
            print(f"[CLIENT] Joined as '{data['name']}' (pid={data['pid']})")

        elif ptype == PKT_GAME_STATE:
            now = time.time()
            with self.lock:
                for pid_str, p in data.get("players", {}).items():
                    pid = int(pid_str)
                    srv_x = float(p["x"])
                    srv_y = float(p["y"])

                    if pid == self.my_pid:
                        # Reconcile our own position
                        last_acked = p.get("last_input_seq", 0)
                        self.reconcile(srv_x, srv_y, last_acked)
                        self.server_players[pid] = p
                    else:
                        # Buffer remote player snapshot for interpolation
                        if pid not in self.interp_buffer:
                            self.interp_buffer[pid] = []
                        self.interp_buffer[pid].append({
                            "t": now,
                            "x": srv_x,
                            "y": srv_y
                        })
                        # Keep buffer size reasonable (last 2 seconds)
                        cutoff = now - 2.0
                        self.interp_buffer[pid] = [
                            s for s in self.interp_buffer[pid] if s["t"] > cutoff
                        ]
                        self.server_players[pid] = p

        elif ptype == PKT_PLAYER_JOIN:
            self._ack(data["seq"])
            pid = data["pid"]
            with self.lock:
                self.server_players[pid] = {
                    "x": data["x"], "y": data["y"],
                    "color": data["color"], "name": data["name"]
                }
                self.interp_buffer[pid] = []
            print(f"[CLIENT] {data['name']} joined")

        elif ptype == PKT_PLAYER_QUIT:
            self._ack(data["seq"])
            pid = data["pid"]
            with self.lock:
                self.server_players.pop(pid, None)
                self.interp_buffer.pop(pid, None)
            print(f"[CLIENT] {data['name']} left")

        elif ptype == PKT_ACK:
            seq = data.get("seq")
            self.pending_acks.pop(seq, None)

        elif ptype == "FULL":
            print("[CLIENT] Server is full!")

    def _ack_retry_loop(self):
        while True:
            now = time.time()
            for seq, info in list(self.pending_acks.items()):
                if now - info["last_sent"] > 0.1:
                    if info["retries"] >= 10:
                        self.pending_acks.pop(seq, None)
                    else:
                        self._send(info["packet"])
                        info["retries"] += 1
                        info["last_sent"] = now
            time.sleep(0.01)


# ─── RENDERER ────────────────────────────────────────────────────────────────

class GameRenderer:
    def __init__(self, client: NetworkClient):
        self.client = client
        pygame.init()
        self.screen = pygame.display.set_mode((WORLD_W, WORLD_H))
        pygame.display.set_caption("UDP Multiplayer — Connecting...")
        self.clock = pygame.time.Clock()
        self.font_lg = pygame.font.SysFont("Consolas", 18, bold=True)
        self.font_sm = pygame.font.SysFont("Consolas", 13)
        self.font_hud = pygame.font.SysFont("Consolas", 14)

        # Trails for local player
        self.trail = []  # list of (x, y)

        # Particle effects for footsteps
        self.particles = []

        # Background surface (pre-rendered grid)
        self.bg = self._make_bg()

    def _make_bg(self):
        surf = pygame.Surface((WORLD_W, WORLD_H))
        surf.fill(BG_COLOR)
        # Grid
        for x in range(0, WORLD_W, 40):
            pygame.draw.line(surf, GRID_COLOR, (x, 0), (x, WORLD_H))
        for y in range(0, WORLD_H, 40):
            pygame.draw.line(surf, GRID_COLOR, (0, y), (WORLD_W, y))
        # Border
        pygame.draw.rect(surf, (60, 60, 100), (0, 0, WORLD_W, WORLD_H), 3)
        # Corner markers
        for cx, cy in [(20, 20), (WORLD_W-20, 20), (20, WORLD_H-20), (WORLD_W-20, WORLD_H-20)]:
            pygame.draw.circle(surf, (80, 80, 120), (cx, cy), 8)
        return surf

    def _draw_player(self, x, y, color_hex, name, is_local=False, alpha=255):
        rgb = hex_to_rgb(color_hex)
        ix, iy = int(x), int(y)

        # Shadow
        shadow_surf = pygame.Surface((40, 20), pygame.SRCALPHA)
        pygame.draw.ellipse(shadow_surf, (0, 0, 0, 60), (0, 0, 40, 20))
        self.screen.blit(shadow_surf, (ix - 20, iy + 10))

        # Body circle
        if is_local:
            # Glow effect
            for r in range(22, 12, -2):
                glow_alpha = max(0, 80 - (22 - r) * 15)
                glow_surf = pygame.Surface((r*2, r*2), pygame.SRCALPHA)
                pygame.draw.circle(glow_surf, (*rgb, glow_alpha), (r, r), r)
                self.screen.blit(glow_surf, (ix - r, iy - r))

        # Main body
        pygame.draw.circle(self.screen, rgb, (ix, iy), 16)
        pygame.draw.circle(self.screen, WHITE, (ix, iy), 16, 2)

        # "Eyes"
        eye_color = (30, 30, 50)
        pygame.draw.circle(self.screen, eye_color, (ix - 5, iy - 4), 3)
        pygame.draw.circle(self.screen, eye_color, (ix + 5, iy - 4), 3)
        pygame.draw.circle(self.screen, WHITE, (ix - 5, iy - 4), 1)
        pygame.draw.circle(self.screen, WHITE, (ix + 5, iy - 4), 1)

        # Name tag
        label = self.font_sm.render(name + (" ★" if is_local else ""), True, rgb)
        bg_rect = label.get_rect(center=(ix, iy - 28))
        bg_surf = pygame.Surface((bg_rect.w + 8, bg_rect.h + 4), pygame.SRCALPHA)
        bg_surf.fill((10, 10, 20, 160))
        self.screen.blit(bg_surf, (bg_rect.x - 4, bg_rect.y - 2))
        self.screen.blit(label, bg_rect)

    def _draw_trail(self):
        for i, (tx, ty) in enumerate(self.trail):
            alpha = int(200 * i / max(len(self.trail), 1))
            r = max(1, int(6 * i / max(len(self.trail), 1)))
            trail_surf = pygame.Surface((r*2, r*2), pygame.SRCALPHA)
            pygame.draw.circle(trail_surf, (0, 255, 170, alpha), (r, r), r)
            self.screen.blit(trail_surf, (int(tx) - r, int(ty) - r))

    def _draw_particles(self):
        for p in self.particles:
            alpha = int(255 * (p["life"] / p["max_life"]))
            ps = pygame.Surface((4, 4), pygame.SRCALPHA)
            pygame.draw.circle(ps, (*hex_to_rgb(p["color"]), alpha), (2, 2), 2)
            self.screen.blit(ps, (int(p["x"]) - 2, int(p["y"]) - 2))

    def _draw_hud(self, fps, my_pid):
        lines = [
            f"FPS: {fps:.0f}",
            f"PID: {my_pid if my_pid is not None else '...'}",
            f"Players: {len(self.client.server_players)}",
            f"Pending ACKs: {len(self.client.pending_acks)}",
            f"Input seq: {self.client.input_seq}",
        ]
        if my_pid is not None:
            with self.client.lock:
                lines.append(f"Pos: ({self.client.local_x:.0f}, {self.client.local_y:.0f})")
                lines.append(f"Unacked inputs: {len(self.client.input_history)}")

        hud_w = 220
        hud_h = len(lines) * 18 + 12
        hud = pygame.Surface((hud_w, hud_h), pygame.SRCALPHA)
        hud.fill((5, 5, 15, 200))
        pygame.draw.rect(hud, (50, 50, 100), (0, 0, hud_w, hud_h), 1)
        for i, line in enumerate(lines):
            txt = self.font_hud.render(line, True, (150, 200, 255))
            hud.blit(txt, (8, 6 + i * 18))
        self.screen.blit(hud, (8, 8))

    def _draw_legend(self):
        controls = [
            "WASD / Arrow keys: Move",
            "ESC: Quit"
        ]
        y = WORLD_H - 14 * len(controls) - 10
        for line in controls:
            txt = self.font_sm.render(line, True, GRAY)
            self.screen.blit(txt, (WORLD_W - txt.get_width() - 10, y))
            y += 14

    def run(self):
        c = self.client
        c.connect()

        keys_prev = set()
        last_input_time = time.time()
        last_trail_time = time.time()

        while True:
            dt = self.clock.tick(60) / 1000.0
            now = time.time()

            # ── EVENTS ───────────────────────────────────────────────────────
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    c.disconnect()
                    pygame.quit()
                    return
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    c.disconnect()
                    pygame.quit()
                    return

            # ── INPUT ────────────────────────────────────────────────────────
            keys = pygame.key.get_pressed()
            dx, dy = 0.0, 0.0
            if keys[pygame.K_w] or keys[pygame.K_UP]:    dy -= 1
            if keys[pygame.K_s] or keys[pygame.K_DOWN]:  dy += 1
            if keys[pygame.K_a] or keys[pygame.K_LEFT]:  dx -= 1
            if keys[pygame.K_d] or keys[pygame.K_RIGHT]: dx += 1

            if c.connected:
                # Predict locally
                c.predict_move(dx, dy, dt)
                # Send to server at INPUT_RATE
                if now - last_input_time >= 1.0 / INPUT_RATE:
                    c.send_input(dx, dy, dt)
                    last_input_time = now

                # Trail
                if (dx != 0 or dy != 0) and now - last_trail_time > 0.05:
                    self.trail.append((c.local_x, c.local_y))
                    if len(self.trail) > 20:
                        self.trail.pop(0)
                    last_trail_time = now
                    # Spawn particle
                    import random
                    self.particles.append({
                        "x": c.local_x + random.uniform(-4, 4),
                        "y": c.local_y + random.uniform(-4, 4),
                        "vx": random.uniform(-20, 20),
                        "vy": random.uniform(-30, 10),
                        "life": 0.4,
                        "max_life": 0.4,
                        "color": "#00FFAA"
                    })
                elif dx == 0 and dy == 0:
                    if self.trail:
                        self.trail.pop(0)

            # ── UPDATE PARTICLES ─────────────────────────────────────────────
            for p in self.particles:
                p["x"] += p["vx"] * dt
                p["y"] += p["vy"] * dt
                p["life"] -= dt
            self.particles = [p for p in self.particles if p["life"] > 0]

            # ── DRAW ─────────────────────────────────────────────────────────
            self.screen.blit(self.bg, (0, 0))
            self._draw_trail()
            self._draw_particles()

            with c.lock:
                my_pid = c.my_pid
                players_snap = dict(c.server_players)
                lx, ly = c.local_x, c.local_y

            # Draw remote players (interpolated)
            for pid, p in players_snap.items():
                if pid == my_pid:
                    continue
                ix, iy = c.get_interpolated_pos(pid, now)
                self._draw_player(ix, iy, p["color"], p["name"], is_local=False)

            # Draw local player (predicted)
            if my_pid is not None and my_pid in players_snap:
                p = players_snap[my_pid]
                self._draw_player(lx, ly, p["color"], p["name"], is_local=True)

            # HUD
            fps = self.clock.get_fps()
            self._draw_hud(fps, my_pid)
            self._draw_legend()

            if not c.connected:
                msg = self.font_lg.render("Connecting to server...", True, (200, 200, 100))
                self.screen.blit(msg, msg.get_rect(center=(WORLD_W//2, WORLD_H//2)))

            pygame.display.flip()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    name = args[0] if len(args) > 0 else "Player"
    host = args[1] if len(args) > 1 else DEFAULT_HOST
    port = int(args[2]) if len(args) > 2 else DEFAULT_PORT

    print(f"[CLIENT] Connecting to {host}:{port} as '{name}'")
    client = NetworkClient(host, port, name)
    renderer = GameRenderer(client)
    renderer.run()
