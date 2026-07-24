# UDP Multiplayer Game

A real-time multiplayer shooter (up to 5 players) built on raw UDP sockets in Python. Demonstrates low-level networking concepts without any game-networking library.

**Core networking features:**
- Client-side prediction — instant local movement, no input lag
- Server reconciliation — corrects mispredictions against authoritative state
- Entity interpolation — smooth remote player movement with 100ms delay buffer
- Custom reliable delivery — ACK/retry layer over UDP for critical packets
- 20 Hz authoritative server tick

**Gameplay features:**
- Shooting with left-click (mouse-aimed)
- Ammo system (10 rounds, 5s reload)
- Health system (150 HP, 20 damage per bullet)
- Kill / Assist / Death tracking
- Scoreboard (Tab)
- Death screen with 5s respawn timer
- In-game chat (T key)
- LAN server browser (auto-discovers servers via UDP broadcast)

---

## Requirements

```bash
pip install pygame
```

No other dependencies — stdlib only otherwise.

---

## Running

Activate the virtual environment first:

```bash
# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### Local play

```bash
# Terminal 1 — server
python server.py

# Terminal 2+ — one per player
python client.py Alice
python client.py Bob
```

### Remote play

```bash
# Server machine
python server.py

# Each client machine
python client.py Alice <server_ip> 9999
```

### Skip the lobby (connect directly)

```bash
python client.py Alice localhost 9999
```

---

## Controls

| Key / Input | Action |
|---|---|
| WASD / Arrow keys | Move |
| Left click | Shoot toward cursor |
| T | Open chat |
| Enter | Send chat message |
| Tab (hold) | Show scoreboard |
| Esc | Close chat / quit |

---

## Gameplay

**Health & Ammo**
- Each player starts with 150 HP and 10 rounds.
- Each bullet hit deals 20 damage.
- When ammo reaches 0, a 5-second reload begins automatically.

**Kill / Assist / Death**
- A kill is awarded to the player who fires the killing shot.
- An assist is awarded to any other player who dealt damage to the victim in the same life.
- Deaths increment on the victim.

**Death & Respawn**
- On death a full-screen overlay appears with a countdown.
- The Respawn button becomes clickable after 5 seconds.
- The server validates the delay independently — early packets are ignored.

**Scoreboard**
- Hold Tab at any time to see all players sorted by kills.
- Columns: Name · Kills · Assists · Deaths.
- Dead players are marked `[dead]`.

---

## Architecture

### File map

| File | Role |
|---|---|
| `server.py` | Authoritative game server — 20 Hz tick, physics, kill tracking |
| `client.py` | Game client — `NetworkClient` (net) + `GameRenderer` (pygame) + `LobbyScreen` |
| `packets.py` | Binary packet codec — the only serialization layer, no JSON |
| `gui.py` | HUD: `TextInput`, `ChatLog`, `Button`, `ScoreBoard`, `DeathOverlay`, `HUD` |

### Networking overview

```
CLIENT                          SERVER                    CLIENT
──────                          ──────                    ──────

[Input keypress]
    │
    ├─► Predict locally (no wait)
    │
    └──── UDP INPUT pkt ────────►
          seq=42, dx=1, dy=0         [Tick loop 20 Hz]
                                     Apply inputs
                                     Simulate physics
                                     Bullet movement
                                     Collision detection
                                          │
          ◄──── GAME_STATE ─────────────┤────────── GAME_STATE ────►
          positions, health,             │
          ammo, kills, dead,             │
          last_acked_input_seq           │
                │
    [Reconcile]
    Discard inputs ≤ last_acked_seq
    Re-simulate remaining inputs
    from server position
```

**Reliable packets** (ACK-tracked, retried every 100ms):
`JOIN_OK`, `PLAYER_JOIN`, `PLAYER_QUIT`

**Unreliable packets** (drop acceptable):
`INPUT`, `GAME_STATE`, `CHAT`, `SHOOT`, `RESPAWN`

### Packet types

| Packet | Dir | Reliable | Description |
|---|---|---|---|
| `CONNECT` | C→S | No | Join request with player name |
| `DISCONNECT` | C→S | No | Graceful leave |
| `ACK` | Both | No | Acknowledges a reliable packet |
| `JOIN_OK` | S→C | **Yes** | Assigned PID, spawn position, existing players |
| `GAME_STATE` | S→C | No | Authoritative world state at 20 Hz |
| `INPUT` | C→S | No | Movement vector + dt (high frequency) |
| `PLAYER_JOIN` | S→C | **Yes** | Another player connected |
| `PLAYER_QUIT` | S→C | **Yes** | Another player disconnected |
| `FULL` | S→C | No | Server at capacity |
| `CHAT` | Both | No | Chat message |
| `SHOOT` | C→S | No | Fire bullet toward normalized (dx, dy) |
| `RESPAWN` | C→S | No | Request respawn (server enforces 5s delay) |
| `SERVER_QUERY` | C→S | No | LAN discovery probe |
| `SERVER_INFO` | S→C | No | Response to discovery probe |

### Client-side prediction & reconciliation

1. **Predict** — on input, move the local player immediately without waiting for the server.
2. **Send** — the input is sent with an incrementing sequence number and the frame `dt`.
3. **Reconcile** — each `GAME_STATE` carries `last_input_seq`. Discard all stored inputs with seq ≤ that value, then re-simulate the remaining unacknowledged inputs on top of the server's authoritative position.

### Entity interpolation

Remote players are rendered with a **100ms delay buffer**. Each `GAME_STATE` snapshot is timestamped and appended to a per-player buffer. At render time, the client interpolates linearly between the two snapshots that bracket `now − 100ms`, smoothing out packet jitter.

### Threading model

**Server** (4 threads):
- `recv_loop` — receives and dispatches all incoming packets
- `ack_retry_loop` — retries unACKed reliable packets every 100ms
- `tick_loop` — physics + broadcast at 20 Hz
- Main thread — status printer / KeyboardInterrupt handler

**Client** (3 threads):
- `_recv_loop` — receives packets, calls `_handle()`
- `_ack_retry_loop` — retries client-side reliable packets
- Main thread — pygame event loop + render at 60 FPS

All shared state is protected by `threading.Lock()`. The server uses a single `lock` for player/bullet state and a separate lock for `pending_acks` to avoid deadlock.

---

## Roadmap

### Done
- Raw UDP transport with custom reliable delivery
- Client-side prediction + server reconciliation
- Entity interpolation
- Binary packet codec (no JSON)
- Shooting, ammo, reload
- Health system
- Kill / Assist / Death tracking
- Scoreboard (Tab)
- Death screen + respawn (5s enforced server-side)
- In-game chat
- LAN server browser

### Phase 3
- Collectibles / scoring system
- Lag compensation
- Replay visualizer
- Delta compression for GAME_STATE
- Visual polish / architecture diagrams
