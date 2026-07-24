"""
UDP Multiplayer Game Server
- Handles up to 2 players
- Reliable ACK system for critical messages
- Broadcasts authoritative game state
- Runs at 20 ticks/sec
"""

import socket
import time
import threading
import struct
import random
import math
from collections import defaultdict
from packets import encode, decode

# CONFIG
HOST = "0.0.0.0"
PORT = 9999
TICK_RATE = 20  # server ticks per second
TICK_INTERVAL = 1.0 / TICK_RATE
MAX_PLAYERS = 5
PLAYER_SPEED = 200.0  # units/sec
PLAYER_RADIUS = 16  # must match client circle size
WORLD_W = 800
WORLD_H = 600
ACK_RETRY_INTERVAL = 0.1
ACK_MAX_RETRIES = 10

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
PKT_RESPAWN = "RESPAWN"
SERVER_NAME = "Game Server"
RESPAWN_DELAY = 5.0  # seconds before player can respawn

SPAWN_POSITIONS = [
    (200, 200),
    (600, 200),
    (400, 300),
    (200, 450),
    (600, 450),
]

PLAYER_COLORS = ["#00FFAA", "#FF6B6B", "#FFD93D", "#6BCBFF", "#FF9FE5"]

BULLET_SPEED  = 500.0   # units/sec
BULLET_RADIUS = 5
MAX_AMMO      = 10
SHOOT_COOLDOWN = 1.0    # seconds between shots
RELOAD_TIME    = 5.0    # seconds to reload empty chamber

# SERVER STATE
players = {}  # pid -> player dict
addr_to_pid = {}  # addr -> pid
next_pid = 0
lock = threading.Lock()
seq_counter = defaultdict(int)  # addr -> outgoing seq
pending_acks = {}  # (addr, seq) -> {packet, retries, last_sent}

bullets = {}      # bid -> {x, y, dx, dy, owner_pid}
next_bullet_id = 0

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.settimeout(0.01)

# HELPERS


def send(addr, data: dict):
    try:
        sock.sendto(encode(data), addr)
    except Exception as e:
        print(f"[SEND ERR] {e}")


def send_reliable(addr, data: dict):
    """Send with ACK tracking. Caller must include 'seq' in data."""
    seq = data["seq"]
    pending_acks[(addr, seq)] = {"packet": data, "retries": 0, "last_sent": time.time()}
    send(addr, data)


def next_seq(addr):
    seq_counter[addr] += 1
    return seq_counter[addr]


def broadcast(data: dict, exclude=None):
    with lock:
        targets = [(pid, p["addr"]) for pid, p in players.items()]
    for pid, addr in targets:
        if addr != exclude:
            send(addr, data)


def broadcast_reliable(data_fn, exclude=None):
    """data_fn(addr) -> dict — so each gets unique seq"""
    with lock:
        targets = [(pid, p["addr"]) for pid, p in players.items()]
    for pid, addr in targets:
        if addr != exclude:
            data = data_fn(addr)
            send_reliable(addr, data)


def validate_input_packet(data: dict) -> bool:
    """Return False if the INPUT packet is malformed or out of range"""
    try:
        dx = float(data["dx"])
        dy = float(data["dy"])
        dt = float(data["dt"])
        seq = int(data["seq"])
    except (KeyError, ValueError, TypeError):
        return False
    if not (-1.0 <= dx <= 1.0 and -1.0 <= dy <= 1.0):
        return False
    if not (-1.0 <= dt <= 1.0):
        return False
    if seq < 0:
        return False
    return True


# ACK RETRY LOOP


def ack_retry_loop():
    while True:
        now = time.time()
        expired = []
        for key, info in list(pending_acks.items()):
            if now - info["last_sent"] > ACK_RETRY_INTERVAL:
                if info["retries"] >= ACK_MAX_RETRIES:
                    expired.append(key)
                else:
                    send(key[0], info["packet"])
                    info["retries"] += 1
                    info["last_sent"] = now
        for key in expired:
            addr = key[0]
            print(f"[ACK TIMEOUT] {addr} seq={key[1]}, dropping.")
            pending_acks.pop(key, None)
            # Optionally disconnect player
            with lock:
                pid = addr_to_pid.get(addr)
            if pid is not None:
                disconnect_player(pid, addr)
        time.sleep(0.01)


# GAME TICK


def tick_loop():
    last = time.time()
    while True:
        now = time.time()
        dt = now - last
        last = now

        with lock:
            for pid, p in players.items():
                if p["dead"]:
                    continue
                # Apply last known input
                inp = p.get("input", {})
                dx = inp.get("dx", 0)
                dy = inp.get("dy", 0)
                if dx != 0 or dy != 0:
                    length = math.sqrt(dx * dx + dy * dy)
                    dx /= length
                    dy /= length
                p["x"] = max(
                    PLAYER_RADIUS,
                    min(WORLD_W - PLAYER_RADIUS, p["x"] + dx * PLAYER_SPEED * dt),
                )
                p["y"] = max(
                    PLAYER_RADIUS,
                    min(WORLD_H - PLAYER_RADIUS, p["y"] + dy * PLAYER_SPEED * dt),
                )
                p["last_input_seq"] = inp.get("seq", 0)

            # COLLISION RESOLUTION (skip dead players)
            pids = [k for k, p in players.items() if not p["dead"]]
            for i in range(len(pids)):
                for j in range(i + 1, len(pids)):
                    a = players[pids[i]]
                    b = players[pids[j]]
                    dx = b["x"] - a["x"]
                    dy = b["y"] - a["y"]
                    dist = math.sqrt(dx * dx + dy * dy)
                    min_dist = PLAYER_RADIUS * 2
                    if 0 < dist < min_dist:
                        overlap = (min_dist - dist) / 2.0
                        nx = dx / dist
                        ny = dy / dist
                        a["x"] = max(
                            PLAYER_RADIUS,
                            min(WORLD_W - PLAYER_RADIUS, a["x"] - nx * overlap),
                        )
                        a["y"] = max(
                            PLAYER_RADIUS,
                            min(WORLD_H - PLAYER_RADIUS, a["y"] - ny * overlap),
                        )
                        b["x"] = max(
                            PLAYER_RADIUS,
                            min(WORLD_W - PLAYER_RADIUS, b["x"] + nx * overlap),
                        )
                        b["y"] = max(
                            PLAYER_RADIUS,
                            min(WORLD_H - PLAYER_RADIUS, b["y"] + ny * overlap),
                        )

            # RELOAD TIMERS
            for p in players.values():
                if p["reloading_since"] is not None:
                    if now - p["reloading_since"] >= RELOAD_TIME:
                        p["ammo"] = MAX_AMMO
                        p["reloading_since"] = None

            # BULLET MOVEMENT + COLLISION
            dead_bullets = []
            for bid, b in bullets.items():
                b["x"] += b["dx"] * BULLET_SPEED * dt
                b["y"] += b["dy"] * BULLET_SPEED * dt

                # Wall collision
                if (b["x"] < BULLET_RADIUS or b["x"] > WORLD_W - BULLET_RADIUS or
                        b["y"] < BULLET_RADIUS or b["y"] > WORLD_H - BULLET_RADIUS):
                    dead_bullets.append(bid)
                    continue

                # Player collision (skip dead players and owner)
                for pid, p in players.items():
                    if pid == b["owner"] or p["dead"]:
                        continue
                    dx = p["x"] - b["x"]
                    dy = p["y"] - b["y"]
                    if math.sqrt(dx*dx + dy*dy) < PLAYER_RADIUS + BULLET_RADIUS:
                        damage = 20
                        attacker_pid = b["owner"]
                        p["health"] = max(0, p["health"] - damage)
                        # Track damage for assists
                        p["damage_dealt"][attacker_pid] = p["damage_dealt"].get(attacker_pid, 0) + damage
                        dead_bullets.append(bid)
                        print(f"[HIT] pid={pid} by pid={attacker_pid}, hp={p['health']}")

                        if p["health"] <= 0 and not p["dead"]:
                            p["dead"] = True
                            p["dead_since"] = now
                            p["deaths"] += 1
                            # Credit kill to attacker
                            if attacker_pid in players:
                                players[attacker_pid]["kills"] += 1
                            # Credit assists: anyone else who dealt damage (not the killer)
                            for dmg_pid in list(p["damage_dealt"].keys()):
                                if dmg_pid != attacker_pid and dmg_pid in players:
                                    players[dmg_pid]["assists"] += 1
                            p["damage_dealt"] = {}
                            print(f"[KILL] pid={pid} killed by pid={attacker_pid}")
                        break

            for bid in dead_bullets:
                bullets.pop(bid, None)

            state = {
                "type": PKT_GAME_STATE,
                "t": now,
                "players": {
                    str(pid): {
                        "x": p["x"],
                        "y": p["y"],
                        "color": p["color"],
                        "name": p["name"],
                        "last_input_seq": p.get("last_input_seq", 0),
                        "health": p.get("health", 150),
                        "ammo": p.get("ammo", MAX_AMMO),
                        "kills": p.get("kills", 0),
                        "assists": p.get("assists", 0),
                        "deaths": p.get("deaths", 0),
                        "dead": p.get("dead", False),
                    }
                    for pid, p in players.items()
                },
                "bullets": {
                    str(bid): {
                        "x": b["x"], "y": b["y"],
                        "owner": b["owner"],
                        "dx": b["dx"], "dy": b["dy"],
                    }
                    for bid, b in bullets.items()
                },
            }

        broadcast(state)

        elapsed = time.time() - now
        sleep_time = TICK_INTERVAL - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)


# PLAYER MANAGEMENT
def disconnect_player(pid, addr):
    with lock:
        if pid not in players:
            return
        name = players[pid]["name"]
        del players[pid]
        addr_to_pid.pop(addr, None)
        print(f"[DISCONNECT] {name} (pid={pid})")

    def make_quit_packet(a):
        return {"type": PKT_PLAYER_QUIT, "pid": pid, "name": name, "seq": next_seq(a)}

    broadcast_reliable(make_quit_packet)


# PACKET HANDLERS
def handle_connect(addr, data):
    global next_pid
    with lock:
        if addr in addr_to_pid:
            return  # already connected
        if len(players) >= MAX_PLAYERS:
            send(addr, {"type": "FULL"})
            return
        pid = next_pid
        next_pid += 1
        spawn = SPAWN_POSITIONS[pid % len(SPAWN_POSITIONS)]
        player = {
            "pid": pid,
            "addr": addr,
            "name": str(data.get("name", f"Player{pid}"))[:20],
            "x": float(spawn[0]),
            "y": float(spawn[1]),
            "color": PLAYER_COLORS[pid % len(PLAYER_COLORS)],
            "input": {},
            "last_input_seq": 0,
            "health": 150,
            "ammo": MAX_AMMO,
            "last_shot_time": 0.0,
            "reloading_since": None,
            "kills": 0,
            "assists": 0,
            "deaths": 0,
            "dead": False,
            "dead_since": None,
            "damage_dealt": {},  # attacker_pid -> total damage this life
        }
        players[pid] = player
        addr_to_pid[addr] = pid
        print(f"[CONNECT] {player['name']} (pid={pid}) from {addr}")

    # Send JOIN_OK reliably
    seq = next_seq(addr)
    send_reliable(
        addr,
        {
            "type": PKT_JOIN_OK,
            "seq": seq,
            "pid": pid,
            "x": player["x"],
            "y": player["y"],
            "color": player["color"],
            "name": player["name"],
            "world_w": WORLD_W,
            "world_h": WORLD_H,
            "existing_players": {
                str(p["pid"]): {
                    "x": p["x"],
                    "y": p["y"],
                    "color": p["color"],
                    "name": p["name"],
                }
                for p in players.values()
                if p["pid"] != pid
            },
        },
    )

    # Notify others
    def make_join_packet(a):
        return {
            "type": PKT_PLAYER_JOIN,
            "seq": next_seq(a),
            "pid": pid,
            "x": player["x"],
            "y": player["y"],
            "color": player["color"],
            "name": player["name"],
        }

    broadcast_reliable(make_join_packet, exclude=addr)


def handle_input(addr, data):
    if not validate_input_packet(data):
        print(f"[WARN] Bad input from {addr},dropping.")
        return
    with lock:
        pid = addr_to_pid.get(addr)
        if pid is None:
            return
        if players[pid].get("dead", False):
            return
        inp_seq = data.get("seq", 0)
        if inp_seq > players[pid].get("last_input_seq", -1):
            players[pid]["input"] = data


def handle_ack(addr, data):
    seq = data.get("seq")
    key = (addr, seq)
    pending_acks.pop(key, None)


def handle_disconnect(addr, data):
    with lock:
        pid = addr_to_pid.get(addr)
    if pid is not None:
        disconnect_player(pid, addr)

def handle_shoot(addr, data):
    global next_bullet_id
    with lock:
        pid = addr_to_pid.get(addr)
        if pid is None:
            return
        p = players[pid]
        now = time.time()

        # Enforce cooldown and ammo
        if p["dead"] or p["ammo"] <= 0 or p["reloading_since"] is not None:
            return
        if now - p["last_shot_time"] < SHOOT_COOLDOWN:
            return

        dx = float(data.get("dx", 0))
        dy = float(data.get("dy", 0))
        length = math.sqrt(dx*dx + dy*dy)
        if length < 1e-6:
            return
        dx /= length
        dy /= length

        bid = next_bullet_id
        next_bullet_id += 1
        bullets[bid] = {
            "x": p["x"], "y": p["y"],
            "dx": dx, "dy": dy,
            "owner": pid,
        }

        p["ammo"] -= 1
        p["last_shot_time"] = now
        if p["ammo"] == 0:
            p["reloading_since"] = now

        print(f"[SHOOT] pid={pid} dir=({dx:.2f},{dy:.2f}) ammo={p['ammo']}")


def handle_respawn(addr, data):
    with lock:
        pid = addr_to_pid.get(addr)
        if pid is None:
            return
        p = players[pid]
        if not p["dead"]:
            return
        if p["dead_since"] is None or time.time() - p["dead_since"] < RESPAWN_DELAY:
            return
        spawn = SPAWN_POSITIONS[pid % len(SPAWN_POSITIONS)]
        p["dead"] = False
        p["dead_since"] = None
        p["health"] = 150
        p["ammo"] = MAX_AMMO
        p["reloading_since"] = None
        p["x"] = float(spawn[0])
        p["y"] = float(spawn[1])
        p["input"] = {}
        print(f"[RESPAWN] pid={pid} '{p['name']}'")


def handle_server_query(addr, data):
    with lock:
        count = len(players)
    send(addr, {
        "type": PKT_SERVER_INFO,
        "player_count": count,
        "max_players": MAX_PLAYERS,
        "server_name": SERVER_NAME,
    })


def handle_chat(addr, data):
    with lock:
        pid = addr_to_pid.get(addr)
        if pid is None:
            return
        name = players[pid]["name"]

    msg = str(data.get("message", "")).strip()
    if not msg or len(msg) > 100:
        return

    print(f"[CHAT] {name}: {msg}")

    broadcast({
        "type": PKT_CHAT,
        "seq": 0,
        "pid": pid,
        "name": name,
        "message": msg,
    })


# RECEIVE LOOP

HANDLERS = {
    PKT_CONNECT: handle_connect,
    PKT_INPUT: handle_input,
    PKT_ACK: handle_ack,
    PKT_DISCONNECT: handle_disconnect,
    PKT_CHAT: handle_chat,
    PKT_SHOOT: handle_shoot,
    PKT_SERVER_QUERY: handle_server_query,
    PKT_RESPAWN: handle_respawn,
}


def recv_loop():
    while True:
        try:
            raw, addr = sock.recvfrom(4096)
            data = decode(raw)
            ptype = data.get("type")

            # PACKET LOG
            with lock:
                pid = addr_to_pid.get(addr)
                name = (
                    players[pid]["name"]
                    if pid is not None and pid in players
                    else "unknown"
                )

            if ptype == PKT_INPUT:
                dx = data.get("dx", 0)
                dy = data.get("dy", 0)
                seq = data.get("seq", "?")
                direction = ""
                if dy < 0:
                    direction += "↑"
                if dy > 0:
                    direction += "↓"
                if dx < 0:
                    direction += "←"
                if dx > 0:
                    direction += "→"
                if not direction:
                    direction = "·"
                print(f"[PKT] INPUT     from {name:<12} seq={seq:<5} dir={direction}")
            elif ptype == PKT_CONNECT:
                join_name = data.get("name", "?")
                print(f"[PKT] CONNECT   from {addr[0]}:{addr[1]}  name='{join_name}'")
            elif ptype == PKT_DISCONNECT:
                print(f"[PKT] DISCONNECT from {name}  addr={addr[0]}:{addr[1]}")
            elif ptype == PKT_ACK:
                seq = data.get("seq", "?")
                print(f"[PKT] ACK       from {name:<12} seq={seq}")
            elif ptype == PKT_CHAT:
                print(f"[PKT] CHAT       from {name:<12} msg='{data.get('message','')[:30]}'")
            elif ptype == PKT_SHOOT:
                print(f"[PKT] SHOOT      from {name:<12}")
            elif ptype == PKT_RESPAWN:
                print(f"[PKT] RESPAWN    from {name:<12}")
            else:
                print(f"[PKT] {ptype:<12} from {addr[0]}:{addr[1]}")
            # END LOG
            if isinstance(ptype, str):
                handler = HANDLERS.get(ptype)
                if handler:
                    handler(addr, data)
        except socket.timeout:
            pass
        except Exception as e:
            pass


# MAIN
if __name__ == "__main__":
    sock.bind((HOST, PORT))
    print(f"[SERVER] Listening on {HOST}:{PORT}")
    print(f"[SERVER] Tick rate: {TICK_RATE} Hz | World: {WORLD_W}x{WORLD_H}")

    threading.Thread(target=recv_loop, daemon=True).start()
    threading.Thread(target=ack_retry_loop, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()

    try:
        while True:
            time.sleep(1)
            with lock:
                names = [p["name"] for p in players.values()]
            print(f"[SERVER] Connected: {names or 'none'}")
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down.")
        sock.close()
