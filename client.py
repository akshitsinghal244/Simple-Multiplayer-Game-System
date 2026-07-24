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
import time
import threading
import sys
import math
import pygame
from packets import encode, decode
from gui import *

# CONFIG
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9999
PLAYER_SPEED = 200.0
PLAYER_RADIUS = 16
WORLD_W = 800
WORLD_H = 600
INPUT_RATE = 60  # inputs per second
INTERP_DELAY = 0.1  # seconds of interpolation buffer for remote players

# PACKET TYPES
PKT_CONNECT = "CONNECT"
PKT_DISCONNECT = "DISCONNECT"
PKT_ACK = "ACK"
PKT_JOIN_OK = "JOIN_OK"
PKT_GAME_STATE = "GAME_STATE"
PKT_INPUT = "INPUT"
PKT_PLAYER_JOIN = "PLAYER_JOIN"
PKT_PLAYER_QUIT = "PLAYER_QUIT"
PKT_CHAT = "CHAT"
PKT_SHOOT = "SHOOT"
PKT_SERVER_QUERY = "SERVER_QUERY"
PKT_SERVER_INFO = "SERVER_INFO"

BULLET_RADIUS = 5
BULLET_COLOR  = (255, 240, 80)

# COLORS
BG_COLOR = (15, 15, 25)
GRID_COLOR = (30, 30, 50)
GROUND_COLOR = (25, 25, 40)
UI_BG = (10, 10, 20, 200)
WHITE = (255, 255, 255)
GRAY = (120, 120, 140)
SHADOW = (0, 0, 0, 80)


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


# NETWORK CLIENT


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
        self._closed = False
        self.lock = threading.Lock()

        # Authoritative state for all players
        self.server_players = {}  # pid -> {x, y, color, name, last_input_seq}

        # Client prediction
        self.local_x = 0.0
        self.local_y = 0.0
        self.input_history = []  # list of {seq, dx, dy, dt, time}
        self.input_seq = 0

        # Interpolation buffer for remote players
        # pid -> list of {t, x, y}
        self.interp_buffer = {}

        # Reliable ACK tracking
        self.pending_acks = {}  # seq -> {packet, retries, last_sent}
        self.out_seq = 0
        self.ack_lock = threading.Lock()

        # World size from server
        self.world_w = WORLD_W
        self.world_h = WORLD_H

        # Bullets from last GAME_STATE
        self.server_bullets = {}  # bid -> {x, y, owner, dx, dy}

        self.ping_ms = 0
        self._ping_send_time = 0

        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._ack_retry_loop, daemon=True).start()

    # SEND HELPERS

    def _encode(self, data):
        return encode(data)

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
        with self.ack_lock:
            self.pending_acks[seq] = {
                "packet": data,
                "retries": 0,
                "last_sent": time.time(),
            }
        self._send(data)

    def _ack(self, seq):
        self._send({"type": PKT_ACK, "seq": seq})

    # CONNECT

    def connect(self):
        self._send({"type": PKT_CONNECT, "name": self.name})

    def disconnect(self):
        self._send({"type": PKT_DISCONNECT})
        self.connected = False
        self.my_pid = None

    def close(self):
        self._closed = True
        try:
            self.sock.close()
        except Exception:
            pass

    def reconnect(self):
        if not self.connected:
            self._send({"type": PKT_CONNECT, "name": self.name})

    def send_shoot(self, dx: float, dy: float):
        if not self.connected:
            return
        self._send({
            "type": PKT_SHOOT,
            "seq": self._next_seq(),
            "dx": dx,
            "dy": dy,
        })

    def send_chat(self, message: str):
        if not self.connected:
            return
        self._send({
            "type": PKT_CHAT,
            "seq": self._next_seq(),
            "pid": self.my_pid or 0,
            "name": self.name,
            "message": message,
        })

    # INPUT SENDING
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
            "t": time.time(),
        }
        # Store for reconciliation
        self.input_history.append(
            {
                "seq": seq,
                "dx": dx,
                "dy": dy,
                "dt": dt,
                "x_before": self.local_x,
                "y_before": self.local_y,
            }
        )
        # Keep history bounded
        if len(self.input_history) > 120:
            self.input_history.pop(0)

        self._send(packet)  # Input is unreliable (high frequency)

    # PREDICTION
    def predict_move(self, dx, dy, dt):
        """Apply movement locally immediately (client-side prediction)."""
        r = PLAYER_RADIUS
        if dx != 0 or dy != 0:
            length = math.sqrt(dx * dx + dy * dy)
            dx /= length
            dy /= length

        self.local_x = max(
            r, min(self.world_w - r, self.local_x + dx * PLAYER_SPEED * dt)
        )
        self.local_y = max(
            r, min(self.world_h - r, self.local_y + dy * PLAYER_SPEED * dt)
        )

    def reconcile(self, server_x, server_y, last_acked_seq):
        """
        Server reconciliation:
        1. Accept server position as ground truth
        2. Re-apply all unacknowledged inputs on top
        """
        # Remove acknowledged inputs
        r = PLAYER_RADIUS
        self.input_history = [
            i for i in self.input_history if i["seq"] > last_acked_seq
        ]

        # Start from server state
        rx, ry = server_x, server_y

        # Re-simulate unacknowledged inputs
        for inp in self.input_history:
            dx, dy = inp["dx"], inp["dy"]
            dt = inp["dt"]
            if dx != 0 or dy != 0:
                length = math.sqrt(dx * dx + dy * dy)
                dx /= length
                dy /= length
            rx = max(r, min(self.world_w - r, rx + dx * PLAYER_SPEED * dt))
            ry = max(r, min(self.world_h - r, ry + dy * PLAYER_SPEED * dt))

        self.local_x = rx
        self.local_y = ry

    # INTERPOLATION
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

    # RECEIVE LOOP
    def _recv_loop(self):
        while not self._closed:
            try:
                raw, _ = self.sock.recvfrom(8192)
                data = decode(raw)
                self._handle(data)
            except socket.timeout:
                pass
            except Exception:
                if self._closed:
                    return

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
                if hasattr(self, "_on_chat"):
                    pass
            print(f"[CLIENT] Joined as '{data['name']}' (pid={data['pid']})")

        elif ptype == PKT_GAME_STATE:
            now = time.time()
            with self.lock:
                for pid_str, p in data.get("players", {}).items():
                    pid = int(pid_str)
                    srv_x = float(p["x"])
                    srv_y = float(p["y"])

                    if pid == self.my_pid:
                        last_acked = p.get("last_input_seq", 0)
                        self.reconcile(srv_x, srv_y, last_acked)
                        self.server_players[pid] = p
                    else:
                        if pid not in self.interp_buffer:
                            self.interp_buffer[pid] = []
                        self.interp_buffer[pid].append(
                            {"t": now, "x": srv_x, "y": srv_y}
                        )
                        cutoff = now - 2.0
                        self.interp_buffer[pid] = [
                            s for s in self.interp_buffer[pid] if s["t"] > cutoff
                        ]
                        self.server_players[pid] = p

                self.server_bullets = data.get("bullets", {})

        elif ptype == PKT_PLAYER_JOIN:
            self._ack(data["seq"])
            pid = data["pid"]
            with self.lock:
                self.server_players[pid] = {
                    "x": data["x"],
                    "y": data["y"],
                    "color": data["color"],
                    "name": data["name"],
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
            with self.ack_lock:
                self.pending_acks.pop(seq, None)

        elif ptype == "FULL":
            print("[CLIENT] Server is full!")

        elif ptype == PKT_CHAT:  # ← add from here
            name = data.get("name", "?")
            msg = data.get("message", "")
            if hasattr(self, "_on_chat"):
                self._on_chat(name, msg)

    def _ack_retry_loop(self):
        while not self._closed:
            now = time.time()
            with self.ack_lock:
                for seq, info in list(self.pending_acks.items()):
                    if now - info["last_sent"] > 0.1:
                        if info["retries"] > 10:
                            self.pending_acks.pop(seq, None)
                        else:
                            self._send(info["packet"])
                            info["retries"] += 1
                            info["last_sent"] = now
            time.sleep(0.01)




# RENDERER


class GameRenderer:
    def __init__(self, client: NetworkClient):
        self.client = client
        pygame.init()
        pygame.font.init()  # explicitly init font module before any font calls
        self.screen = pygame.display.set_mode((WORLD_W, WORLD_H))
        pygame.display.set_caption("UDP Multiplayer — Connecting...")
        self.clock = pygame.time.Clock()
        # We keep the code compatible for both Linux and Windows System.

        self.clock = pygame.time.Clock()

        self._return_to_lobby = False

        def _on_disconnect():
            client.disconnect()
            self._return_to_lobby = True

        self.hud = HUD(
            WORLD_W, WORLD_H,
            on_connect=client.reconnect,
            on_disconnect=_on_disconnect,
        )
        client._on_chat = self.hud.add_chat  # wire chat arrival → log


        self.font_lg = pygame.font.Font(None, 22)
        self.font_sm = pygame.font.Font(None, 16)
        self.font_hud = pygame.font.Font(None, 18)

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
        for cx, cy in [
            (20, 20),
            (WORLD_W - 20, 20),
            (20, WORLD_H - 20),
            (WORLD_W - 20, WORLD_H - 20),
        ]:
            pygame.draw.circle(surf, (80, 80, 120), (cx, cy), 8)
        return surf

    def _draw_health_bar(self, x, y, health):
        MAX_HEALTH = 150
        BAR_W = 36
        BAR_H = 4
        bx = int(x) - BAR_W // 2
        by = int(y) - 42
        filled = int(BAR_W * max(0, health) / MAX_HEALTH)
        ratio = max(0, health) / MAX_HEALTH
        r = int(255 * (1 - ratio))
        g = int(200 * ratio)
        bar_color = (r, g, 40)
        pygame.draw.rect(self.screen, (30, 30, 30), (bx, by, BAR_W, BAR_H))
        if filled > 0:
            pygame.draw.rect(self.screen, bar_color, (bx, by, filled, BAR_H))

    def _draw_player(self, x, y, color_hex, name, health=150, is_local=False, alpha=255):
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
                glow_surf = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
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

        # Health bar
        self._draw_health_bar(x, y, health)

        # Name tag
        label = self.font_sm.render(name + (" ★" if is_local else ""), True, rgb)
        bg_rect = label.get_rect(center=(ix, iy - 28))
        bg_surf = pygame.Surface((bg_rect.w + 8, bg_rect.h + 4), pygame.SRCALPHA)
        bg_surf.fill((10, 10, 20, 160))
        self.screen.blit(bg_surf, (bg_rect.x - 4, bg_rect.y - 2))
        self.screen.blit(label, bg_rect)

    def _draw_bullets(self, bullets_snap):
        for b in bullets_snap.values():
            bx, by = int(b["x"]), int(b["y"])
            # Glow
            glow = pygame.Surface((18, 18), pygame.SRCALPHA)
            pygame.draw.circle(glow, (255, 240, 80, 60), (9, 9), 9)
            self.screen.blit(glow, (bx - 9, by - 9))
            pygame.draw.circle(self.screen, BULLET_COLOR, (bx, by), BULLET_RADIUS)
            pygame.draw.circle(self.screen, (255, 255, 255), (bx, by), BULLET_RADIUS, 1)

    def _draw_trail(self):
        for i, (tx, ty) in enumerate(self.trail):
            alpha = int(200 * i / max(len(self.trail), 1))
            r = max(1, int(6 * i / max(len(self.trail), 1)))
            trail_surf = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
            pygame.draw.circle(trail_surf, (0, 255, 170, alpha), (r, r), r)
            self.screen.blit(trail_surf, (int(tx) - r, int(ty) - r))

    def _draw_particles(self):
        for p in self.particles:
            alpha = int(255 * (p["life"] / p["max_life"]))
            ps = pygame.Surface((4, 4), pygame.SRCALPHA)
            pygame.draw.circle(ps, (*hex_to_rgb(p["color"]), alpha), (2, 2), 2)
            self.screen.blit(ps, (int(p["x"]) - 2, int(p["y"]) - 2))

    def _draw_ammo_bar(self, ammo: int, reloading: bool):
        MAX_AMMO = 10
        x, y = 8, self.screen.get_height() - 148
        font = self.font_sm
        if reloading:
            label = font.render("RELOADING…", True, (255, 160, 40))
        else:
            label = font.render(f"Ammo: {ammo}/{MAX_AMMO}", True, (220, 224, 232))
        self.screen.blit(label, (x, y))
        y += 14
        for i in range(MAX_AMMO):
            color = (255, 220, 50) if i < ammo else (50, 50, 60)
            pygame.draw.rect(self.screen, color, (x + i * 14, y, 10, 6), border_radius=2)

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
                lines.append(
                    f"Pos: ({self.client.local_x:.0f}, {self.client.local_y:.0f})"
                )
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
        controls = ["WASD / Arrow keys: Move", "Left Click: Shoot", "ESC: Quit"]
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

            if self._return_to_lobby:
                c.close()
                return "lobby"

            # EVENTS
            for event in pygame.event.get():
                self.hud.handle_event(event)
                if event.type == pygame.QUIT:
                    c.disconnect()
                    c.close()
                    pygame.quit()
                    return
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    if self.hud.is_chat_open():
                        pass
                    elif self.hud.is_suppressed():
                        pass
                    else:
                        c.disconnect()
                        c.close()
                        pygame.quit()
                        return
                if (event.type == pygame.MOUSEBUTTONDOWN and event.button == 1
                        and c.connected and not self.hud.is_suppressed()):
                    mx, my = event.pos
                    with c.lock:
                        ox, oy = c.local_x, c.local_y
                    ddx = mx - ox
                    ddy = my - oy
                    length = math.sqrt(ddx*ddx + ddy*ddy)
                    if length > 1e-6:
                        c.send_shoot(ddx / length, ddy / length)

            # INPUT
            keys = pygame.key.get_pressed()
            dx, dy = 0.0, 0.0
            if not self.hud.is_chat_open():
                if not self.hud.is_suppressed():
                    if keys[pygame.K_w] or keys[pygame.K_UP]:
                        dy -= 1
                    if keys[pygame.K_s] or keys[pygame.K_DOWN]:
                        dy += 1
                    if keys[pygame.K_a] or keys[pygame.K_LEFT]:
                        dx -= 1
                    if keys[pygame.K_d] or keys[pygame.K_RIGHT]:
                        dx += 1

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

                    self.particles.append(
                        {
                            "x": c.local_x + random.uniform(-4, 4),
                            "y": c.local_y + random.uniform(-4, 4),
                            "vx": random.uniform(-20, 20),
                            "vy": random.uniform(-30, 10),
                            "life": 0.4,
                            "max_life": 0.4,
                            "color": "#00FFAA",
                        }
                    )
                elif dx == 0 and dy == 0:
                    if self.trail:
                        self.trail.pop(0)

            # UPDATE PARTICLES
            for p in self.particles:
                p["x"] += p["vx"] * dt
                p["y"] += p["vy"] * dt
                p["life"] -= dt
            self.particles = [p for p in self.particles if p["life"] > 0]

            # DRAW
            self.screen.blit(self.bg, (0, 0))
            self._draw_trail()
            self._draw_particles()

            with c.lock:
                my_pid = c.my_pid
                players_snap = dict(c.server_players)
                lx, ly = c.local_x, c.local_y
                bullets_snap = dict(c.server_bullets)

            # Draw bullets
            self._draw_bullets(bullets_snap)

            # Draw remote players (interpolated)
            for pid, p in players_snap.items():
                if pid == my_pid:
                    continue
                ix, iy = c.get_interpolated_pos(pid, now)
                self._draw_player(ix, iy, p["color"], p["name"],
                                  health=p.get("health", 150), is_local=False)

            # Draw local player (predicted)
            if my_pid is not None and my_pid in players_snap:
                p = players_snap[my_pid]
                self._draw_player(lx, ly, p["color"], p["name"],
                                  health=p.get("health", 150), is_local=True)

            # HUD
            fps = self.clock.get_fps()
            self._draw_hud(fps, my_pid)
            self._draw_legend()

            # Ammo bar
            if my_pid is not None and my_pid in players_snap:
                my_p = players_snap[my_pid]
                ammo = my_p.get("ammo", 10)
                reloading = (ammo == 0)
                self._draw_ammo_bar(ammo, reloading)
            self.hud.set_state("connected" if c.connected else "disconnected")
            self.hud.tick(dt)
            msg = self.hud.consume_chat()
            if msg:
                c.send_chat(msg)
            self.hud.draw(self.screen)

            if not c.connected:
                msg = self.font_lg.render(
                    "Connecting to server...", True, (200, 200, 100)
                )
                self.screen.blit(msg, msg.get_rect(center=(WORLD_W // 2, WORLD_H // 2)))

            pygame.display.flip()


# LOBBY

LOBBY_W = 700
LOBBY_H = 520
SCAN_INTERVAL = 2.0        # seconds between auto-rescans
SCAN_TIMEOUT  = 1.0        # seconds to wait for responses per scan
SCAN_SUBNETS  = [          # broadcast addresses to probe
    "255.255.255.255",
    "127.0.0.1",
]


class LobbyScreen:
    """
    Pre-game server browser.
    Scans LAN via UDP broadcast for SERVER_QUERY and lets the user
    type a manual IP, then returns (host, port, name) when the user
    clicks Join.
    """

    _ROW_H      = 52
    _LIST_X     = 30
    _LIST_Y     = 130
    _LIST_W     = 640
    _MAX_ROWS   = 5

    def __init__(self, player_name: str):
        self.player_name = player_name

        pygame.init()
        pygame.font.init()
        self.screen = pygame.display.set_mode((LOBBY_W, LOBBY_H))
        pygame.display.set_caption("Server Browser")
        self.clock = pygame.time.Clock()

        self._font_title = pygame.font.Font(None, 44)
        self._font_hd    = pygame.font.Font(None, 18)
        self._font_body  = pygame.font.Font(None, 20)
        self._font_small = pygame.font.Font(None, 16)

        # Discovered servers: list of dicts
        # {host, port, server_name, player_count, max_players, last_seen}
        self._servers: list[dict] = []
        self._lock = threading.Lock()
        self._selected = -1   # index into self._servers

        # Manual IP entry
        self._manual_host = DEFAULT_HOST
        self._manual_port = str(DEFAULT_PORT)
        self._editing = None  # "host" | "port" | None

        # Status line
        self._status = "Scanning…"

        # UDP socket for discovery
        self._disc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._disc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._disc_sock.settimeout(0.02)
        self._disc_sock.bind(("", 0))

        self._last_scan = 0.0
        threading.Thread(target=self._recv_loop, daemon=True).start()

        # result: (host, port) or None
        self._result: "tuple[str,int] | None" = None

    # DISCOVERY

    def _send_query(self):
        pkt = encode({"type": PKT_SERVER_QUERY})
        for addr in SCAN_SUBNETS:
            try:
                self._disc_sock.sendto(pkt, (addr, DEFAULT_PORT))
            except Exception:
                pass
        self._status = "Scanning…"

    def _recv_loop(self):
        while True:
            try:
                raw, (host, port) = self._disc_sock.recvfrom(256)
                data = decode(raw)
                if data.get("type") == PKT_SERVER_INFO:
                    entry = {
                        "host": host,
                        "port": port,
                        "server_name": data.get("server_name", "Game Server"),
                        "player_count": data.get("player_count", 0),
                        "max_players": data.get("max_players", 5),
                        "last_seen": time.time(),
                    }
                    with self._lock:
                        for i, s in enumerate(self._servers):
                            if s["host"] == host and s["port"] == port:
                                self._servers[i] = entry
                                break
                        else:
                            self._servers.append(entry)
                        self._status = f"{len(self._servers)} server(s) found"
            except socket.timeout:
                pass
            except Exception:
                pass

    def _prune_stale(self):
        cutoff = time.time() - 8.0
        with self._lock:
            self._servers = [s for s in self._servers if s["last_seen"] > cutoff]
            if self._selected >= len(self._servers):
                self._selected = -1

    # DRAWING HELPERS

    def _draw_row(self, surf, idx, entry, selected):
        x = self._LIST_X
        y = self._LIST_Y + idx * self._ROW_H
        w = self._LIST_W
        h = self._ROW_H - 4

        bg = (30, 40, 55) if selected else (18, 22, 32)
        border = (0, 200, 130) if selected else (40, 50, 70)
        pygame.draw.rect(surf, bg, (x, y, w, h), border_radius=6)
        pygame.draw.rect(surf, border, (x, y, w, h), 1, border_radius=6)

        # Server name
        name_surf = self._font_body.render(entry["server_name"], True, (220, 224, 232))
        surf.blit(name_surf, (x + 14, y + 8))

        # Address
        addr_str = f"{entry['host']}:{entry['port']}"
        addr_surf = self._font_small.render(addr_str, True, (100, 110, 130))
        surf.blit(addr_surf, (x + 14, y + 28))

        # Player count — right-aligned
        count_str = f"{entry['player_count']}/{entry['max_players']} players"
        full = entry["player_count"] >= entry["max_players"]
        count_color = (255, 80, 80) if full else (0, 220, 140)
        count_surf = self._font_body.render(count_str, True, count_color)
        surf.blit(count_surf, (x + w - count_surf.get_width() - 14, y + 17))

    def _draw_input_field(self, surf, label, value, rect, active):
        border_col = (0, 200, 130) if active else (50, 60, 80)
        pygame.draw.rect(surf, (18, 22, 32), rect, border_radius=4)
        pygame.draw.rect(surf, border_col, rect, 1, border_radius=4)
        lbl = self._font_small.render(label, True, (100, 110, 130))
        surf.blit(lbl, (rect.x, rect.y - 16))
        cursor = "|" if active and (int(time.time() * 2) % 2 == 0) else ""
        val_surf = self._font_body.render(value + cursor, True, (220, 224, 232))
        surf.blit(val_surf, (rect.x + 8, rect.y + (rect.h - val_surf.get_height()) // 2))

    def _draw_button(self, surf, rect, label, enabled):
        if enabled:
            col = (0, 180, 110)
            tc  = (10, 10, 20)
        else:
            col = (35, 40, 55)
            tc  = (70, 80, 95)
        pygame.draw.rect(surf, col, rect, border_radius=6)
        ls = self._font_body.render(label, True, tc)
        surf.blit(ls, (rect.centerx - ls.get_width() // 2,
                       rect.centery - ls.get_height() // 2))

    # MAIN LOOP

    def run(self) -> "tuple[str, int] | None":
        """Blocks until the user picks a server or quits. Returns (host, port)."""

        host_rect = pygame.Rect(self._LIST_X, LOBBY_H - 90, 280, 28)
        port_rect = pygame.Rect(self._LIST_X + 310, LOBBY_H - 90, 100, 28)
        join_rect = pygame.Rect(self._LIST_X + 440, LOBBY_H - 96, 230, 40)
        rs_rect   = pygame.Rect(LOBBY_W - 120, 62, 90, 24)

        while True:
            dt = self.clock.tick(60) / 1000.0
            now = time.time()

            # Auto-rescan
            if now - self._last_scan > SCAN_INTERVAL:
                self._last_scan = now
                self._send_query()
            self._prune_stale()

            # EVENTS
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    return None

                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        pygame.quit()
                        return None
                    if event.key == pygame.K_RETURN:
                        result = self._try_join()
                        if result:
                            return result
                    if self._editing == "host":
                        if event.key == pygame.K_BACKSPACE:
                            self._manual_host = self._manual_host[:-1]
                        elif event.unicode and event.unicode.isprintable() and len(self._manual_host) < 40:
                            self._manual_host += event.unicode
                    elif self._editing == "port":
                        if event.key == pygame.K_BACKSPACE:
                            self._manual_port = self._manual_port[:-1]
                        elif event.unicode.isdigit() and len(self._manual_port) < 5:
                            self._manual_port += event.unicode

                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    mx, my = event.pos

                    # Click on server list row
                    for i in range(min(len(self._servers), self._MAX_ROWS)):
                        rx = self._LIST_X
                        ry = self._LIST_Y + i * self._ROW_H
                        rw = self._LIST_W
                        rh = self._ROW_H - 4
                        if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                            if self._selected == i:
                                # double-click: join immediately
                                result = self._join_selected()
                                if result:
                                    return result
                            else:
                                self._selected = i
                                with self._lock:
                                    if i < len(self._servers):
                                        s = self._servers[i]
                                        self._manual_host = s["host"]
                                        self._manual_port = str(s["port"])
                            break

                    # Click on text fields
                    if rs_rect.collidepoint(mx, my):
                        self._send_query()
                    elif host_rect.collidepoint(mx, my):
                        self._editing = "host"
                    elif port_rect.collidepoint(mx, my):
                        self._editing = "port"
                    elif join_rect.collidepoint(mx, my):
                        result = self._try_join()
                        if result:
                            return result
                    else:
                        self._editing = None

            # DRAW
            self.screen.fill((10, 12, 20))

            # Title
            title = self._font_title.render("Server Browser", True, (0, 220, 140))
            self.screen.blit(title, (self._LIST_X, 24))

            # Subtitle / status
            sub = self._font_small.render(self._status, True, (80, 100, 120))
            self.screen.blit(sub, (self._LIST_X, 72))

            # Rescan button
            self._draw_button(self.screen, rs_rect, "Rescan", True)

            # Column headers
            hdr_y = self._LIST_Y - 20
            hdr = self._font_small.render("SERVER NAME", True, (60, 80, 100))
            self.screen.blit(hdr, (self._LIST_X + 14, hdr_y))
            hdr2 = self._font_small.render("PLAYERS", True, (60, 80, 100))
            self.screen.blit(hdr2, (self._LIST_X + self._LIST_W - hdr2.get_width() - 14, hdr_y))

            # Server list
            with self._lock:
                servers_snap = list(self._servers[:self._MAX_ROWS])
            if servers_snap:
                for i, entry in enumerate(servers_snap):
                    self._draw_row(self.screen, i, entry, i == self._selected)
            else:
                empty = self._font_body.render("No servers found — enter an address manually below",
                                               True, (60, 75, 95))
                ey = self._LIST_Y + self._ROW_H
                self.screen.blit(empty, (self._LIST_X + 14, ey))

            # Separator
            sep_y = LOBBY_H - 115
            pygame.draw.line(self.screen, (30, 38, 55),
                             (self._LIST_X, sep_y), (LOBBY_W - self._LIST_X, sep_y))

            # Manual entry fields
            self._draw_input_field(self.screen, "Host / IP", self._manual_host,
                                   host_rect, self._editing == "host")
            self._draw_input_field(self.screen, "Port", self._manual_port,
                                   port_rect, self._editing == "port")

            # Join button
            can_join = bool(self._manual_host and self._manual_port)
            self._draw_button(self.screen, join_rect, "Join Server", can_join)

            pygame.display.flip()

        return None

    def _join_selected(self) -> "tuple[str, int] | None":
        with self._lock:
            if 0 <= self._selected < len(self._servers):
                s = self._servers[self._selected]
                return s["host"], s["port"]
        return None

    def _try_join(self) -> "tuple[str, int] | None":
        host = self._manual_host.strip()
        try:
            port = int(self._manual_port.strip())
        except ValueError:
            return None
        if host:
            return host, port
        return None

    def close(self):
        try:
            self._disc_sock.close()
        except Exception:
            pass


# MAIN

if __name__ == "__main__":
    args = sys.argv[1:]
    name = args[0] if len(args) > 0 else "Player"

    # If host+port are given on the CLI, skip the lobby and connect directly
    if len(args) >= 3:
        host = args[1]
        port = int(args[2])
    else:
        lobby = LobbyScreen(name)
        result = lobby.run()
        lobby.close()
        if result is None:
            sys.exit(0)
        host, port = result

    while True:
        print(f"[CLIENT] Connecting to {host}:{port} as '{name}'")
        client = NetworkClient(host, port, name)
        renderer = GameRenderer(client)
        result = renderer.run()
        if result != "lobby":
            break

        # Back to lobby — let the user pick a new server
        lobby = LobbyScreen(name)
        pick = lobby.run()
        lobby.close()
        if pick is None:
            break
        host, port = pick
