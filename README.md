# UDP Multiplayer Game

A two-player real-time game using raw UDP sockets with:
- **Client-side prediction** (instant local movement)
- **Server reconciliation** (corrects mispredictions)
- **Entity interpolation** (smooth remote player movement)
- **Reliable ACK system** (for critical packets like join/quit)
- **20 Hz authoritative server tick**

---

## Requirements

```bash
pip install pygame
```

---

## Running

### 1. Start the Server
```bash
python server.py
```

### 2. Start Two Clients (separate terminals)
```bash
python client.py Alice
python client.py Bob
```

### Remote play
```bash
# On the server machine
python server.py

# On each client machine
python client.py Alice <server_ip> 9999
python client.py Bob <server_ip> 9999
```

---

## Controls

| Key | Action |
|-----|--------|
| WASD or Arrow Keys | Move |
| ESC | Quit |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     NETWORK SYSTEM                          │
│                                                             │
│  CLIENT A                     SERVER                CLIENT B │
│  ────────                    ────────               ──────── │
│                                                             │
│  [Input]                                                    │
│     │  ──── UDP INPUT pkt ──────────────────────────────►   │
│     │       (seq=42, dx=1, dy=0, dt=0.016)                 │
│     ▼                          │                            │
│  [Predict]                     ▼                            │
│  Move locally              [Tick Loop 20Hz]                 │
│  IMMEDIATELY               Apply inputs                     │
│                            Simulate physics                 │
│                             │                               │
│                             ├──── GAME_STATE ────────────►  │
│                             │     (all players, positions,  │
│  [Reconcile] ◄─────────────┘      last_acked_input_seq)    │
│  Re-apply                                                   │
│  unacked                                                    │
│  inputs                                                     │
│                                                             │
│  RELIABLE PACKETS (JOIN_OK, PLAYER_JOIN, PLAYER_QUIT):      │
│  ── Sent with seq number                                    │
│  ── Retried every 100ms until ACK received                  │
│  ── Client ACKs immediately on receipt                      │
└─────────────────────────────────────────────────────────────┘
```

### Packet Types

| Packet | Direction | Reliable | Description |
|--------|-----------|----------|-------------|
| `CONNECT` | C→S | No | Join request |
| `JOIN_OK` | S→C | **Yes** | Assigned PID, spawn pos |
| `INPUT` | C→S | No | Movement input (high freq) |
| `GAME_STATE` | S→C | No | Authoritative positions (20Hz) |
| `PLAYER_JOIN` | S→C | **Yes** | Another player connected |
| `PLAYER_QUIT` | S→C | **Yes** | Another player left |
| `ACK` | Both | No | Acknowledges reliable packet |
| `DISCONNECT` | C→S | No | Graceful disconnect |

### Client-Side Prediction

1. **Predict**: When you press a key, move your character locally *immediately* without waiting for the server.
2. **Send**: The input is sent to the server with an incrementing sequence number and the `dt`.
3. **Reconcile**: When a `GAME_STATE` arrives, it includes `last_input_seq` — the last input the server processed. Discard all inputs ≤ that seq, then re-simulate remaining unacknowledged inputs on top of the server's authoritative position.

### Entity Interpolation

Remote players are rendered with a **100ms delay buffer**. Snapshots are buffered, and we linearly interpolate between the two nearest snapshots at `now - 100ms`. This smooths out packet jitter for remote entities.
