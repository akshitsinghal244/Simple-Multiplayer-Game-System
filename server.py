"""
UDP Multiplayer Game Server
- Handles up to 2 players
- Reliable ACK system for critical messages
- Broadcasts authoritative game state
- Runs at 20 ticks/sec
"""

import socket
import json
import time
import threading
import struct
import random
import math
from collections import defaultdict

# ─── CONFIG ──────────────────────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 9999
TICK_RATE = 20          # server ticks per second
TICK_INTERVAL = 1.0 / TICK_RATE
MAX_PLAYERS = 2
PLAYER_SPEED = 200.0    # units/sec
WORLD_W = 800
WORLD_H = 600
ACK_RETRY_INTERVAL = 0.1
ACK_MAX_RETRIES = 10

# ─── PACKET TYPES ────────────────────────────────────────────────────────────
PKT_CONNECT     = "CONNECT"
PKT_DISCONNECT  = "DISCONNECT"
PKT_ACK         = "ACK"
PKT_JOIN_OK     = "JOIN_OK"
PKT_GAME_STATE  = "GAME_STATE"
PKT_INPUT       = "INPUT"
PKT_PLAYER_JOIN = "PLAYER_JOIN"
PKT_PLAYER_QUIT = "PLAYER_QUIT"

SPAWN_POSITIONS = [
    (200, 300),
    (600, 300),
]

PLAYER_COLORS = ["#00FFAA", "#FF6B6B"]

# ─── SERVER STATE ─────────────────────────────────────────────────────────────
players = {}        # pid -> player dict
addr_to_pid = {}    # addr -> pid
next_pid = 0
lock = threading.Lock()
seq_counter = defaultdict(int)    # addr -> outgoing seq
pending_acks = {}                 # (addr, seq) -> {packet, retries, last_sent}

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.settimeout(0.01)

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def encode(data: dict) -> bytes:
    return json.dumps(data).encode()

def decode(raw: bytes) -> dict:
    return json.loads(raw.decode())

def send(addr, data: dict):
    try:
        sock.sendto(encode(data), addr)
    except Exception as e:
        print(f"[SEND ERR] {e}")

def send_reliable(addr, data: dict):
    """Send with ACK tracking. Caller must include 'seq' in data."""
    seq = data["seq"]
    pending_acks[(addr, seq)] = {
        "packet": data,
        "retries": 0,
        "last_sent": time.time()
    }
    send(addr, data)

def next_seq(addr):
    seq_counter[addr] += 1
    return seq_counter[addr]

def broadcast(data: dict, exclude=None):
    with lock:
        targets = list(players.keys())
    for pid in targets:
        addr = players[pid]["addr"]
        if addr != exclude:
            send(addr, data)

def broadcast_reliable(data_fn, exclude=None):
    """data_fn(addr) -> dict — so each gets unique seq"""
    with lock:
        targets = list(players.keys())
    for pid in targets:
        addr = players[pid]["addr"]
        if addr != exclude:
            data = data_fn(addr)
            send_reliable(addr, data)

# ─── ACK RETRY LOOP ──────────────────────────────────────────────────────────

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

# ─── GAME TICK ────────────────────────────────────────────────────────────────

def tick_loop():
    last = time.time()
    while True:
        now = time.time()
        dt = now - last
        last = now

        with lock:
            for pid, p in players.items():
                # Apply last known input
                inp = p.get("input", {})
                dx = inp.get("dx", 0)
                dy = inp.get("dy", 0)
                if dx != 0 or dy != 0:
                    length = math.sqrt(dx*dx + dy*dy)
                    dx /= length
                    dy /= length
                p["x"] = max(20, min(WORLD_W - 20, p["x"] + dx * PLAYER_SPEED * dt))
                p["y"] = max(20, min(WORLD_H - 20, p["y"] + dy * PLAYER_SPEED * dt))
                p["last_input_seq"] = inp.get("seq", 0)

            state = {
                "type": PKT_GAME_STATE,
                "t": now,
                "players": {
                    str(pid): {
                        "x": p["x"],
                        "y": p["y"],
                        "color": p["color"],
                        "name": p["name"],
                        "last_input_seq": p.get("last_input_seq", 0)
                    }
                    for pid, p in players.items()
                }
            }

        broadcast(state)

        elapsed = time.time() - now
        sleep_time = TICK_INTERVAL - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

# ─── PLAYER MANAGEMENT ────────────────────────────────────────────────────────

def disconnect_player(pid, addr):
    with lock:
        if pid not in players:
            return
        name = players[pid]["name"]
        del players[pid]
        addr_to_pid.pop(addr, None)
        print(f"[DISCONNECT] {name} (pid={pid})")

    def make_quit_packet(a):
        return {
            "type": PKT_PLAYER_QUIT,
            "pid": pid,
            "name": name,
            "seq": next_seq(a)
        }
    broadcast_reliable(make_quit_packet)

# ─── PACKET HANDLERS ──────────────────────────────────────────────────────────

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
            "name": data.get("name", f"Player{pid}"),
            "x": float(spawn[0]),
            "y": float(spawn[1]),
            "color": PLAYER_COLORS[pid % len(PLAYER_COLORS)],
            "input": {},
            "last_input_seq": 0
        }
        players[pid] = player
        addr_to_pid[addr] = pid
        print(f"[CONNECT] {player['name']} (pid={pid}) from {addr}")

    # Send JOIN_OK reliably
    seq = next_seq(addr)
    send_reliable(addr, {
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
                "x": p["x"], "y": p["y"],
                "color": p["color"], "name": p["name"]
            }
            for p in players.values() if p["pid"] != pid
        }
    })

    # Notify others
    def make_join_packet(a):
        return {
            "type": PKT_PLAYER_JOIN,
            "seq": next_seq(a),
            "pid": pid,
            "x": player["x"],
            "y": player["y"],
            "color": player["color"],
            "name": player["name"]
        }
    broadcast_reliable(make_join_packet, exclude=addr)


def handle_input(addr, data):
    with lock:
        pid = addr_to_pid.get(addr)
        if pid is None:
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

# ─── RECEIVE LOOP ─────────────────────────────────────────────────────────────

HANDLERS = {
    PKT_CONNECT:    handle_connect,
    PKT_INPUT:      handle_input,
    PKT_ACK:        handle_ack,
    PKT_DISCONNECT: handle_disconnect,
}

def recv_loop():
    while True:
        try:
            raw, addr = sock.recvfrom(4096)
            data = decode(raw)
            ptype = data.get("type")
            handler = HANDLERS.get(ptype)
            if handler:
                handler(addr, data)
        except socket.timeout:
            pass
        except Exception as e:
            pass

# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sock.bind((HOST, PORT))
    print(f"[SERVER] Listening on {HOST}:{PORT}")
    print(f"[SERVER] Tick rate: {TICK_RATE} Hz | World: {WORLD_W}x{WORLD_H}")

    threading.Thread(target=recv_loop,      daemon=True).start()
    threading.Thread(target=ack_retry_loop, daemon=True).start()
    threading.Thread(target=tick_loop,      daemon=True).start()

    try:
        while True:
            time.sleep(1)
            with lock:
                names = [p["name"] for p in players.values()]
            print(f"[SERVER] Connected: {names or 'none'}")
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down.")
        sock.close()
