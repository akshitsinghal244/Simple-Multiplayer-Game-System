"""
packets.py — Binary packet codec for UDP Multiplayer Game

Replaces JSON with compact struct-based binary format.
First byte of every packet is always the type ID.
All multi-byte values use network byte order (big-endian).

Public API (drop-in for json encode/decode):
    encode(data: dict) -> bytes
    decode(raw:  bytes) -> dict
"""

import struct

# Layout constants
NAME_LEN  = 20   # bytes per name field (null-padded UTF-8)
COLOR_LEN = 3    # bytes per color field (raw RGB)

# Packet type IDs (first byte on the wire)
_ID: dict[str, int] = {
    "CONNECT":     0x01,
    "DISCONNECT":  0x02,
    "ACK":         0x03,
    "JOIN_OK":     0x04,
    "GAME_STATE":  0x05,
    "INPUT":       0x06,
    "PLAYER_JOIN": 0x07,
    "PLAYER_QUIT": 0x08,
    "FULL":        0x09,
}
_TYPE: dict[int, str] = {v: k for k, v in _ID.items()}

# Struct format strings  (! = big-endian / network order)
# B  = uint8   (1 byte)   H  = uint16  (2 bytes)
# I  = uint32  (4 bytes)  f  = float32 (4 bytes)
# d  = float64 (8 bytes)  Ns = N raw bytes

_FMT_CONNECT     = f"!B{NAME_LEN}s"                        # 21 B
_FMT_DISCONNECT  = "!B"                                      # 1 B
_FMT_ACK         = "!BI"                                     # 5 B
_FMT_INPUT       = "!BIfff"                                  # 17 B
_FMT_PLAYER_REC  = f"!Bff{COLOR_LEN}s{NAME_LEN}sI"         # 36 B
_FMT_GAME_HDR    = "!BdB"                                    # 10 B  + N×36
_FMT_JOIN_OK_HDR = f"!BIBff{COLOR_LEN}s{NAME_LEN}sHHB"     # 42 B  + N×36
_FMT_PLAYER_JOIN = f"!BIBff{COLOR_LEN}s{NAME_LEN}s"        # 37 B
_FMT_PLAYER_QUIT = f"!BIB{NAME_LEN}s"                       # 26 B

_PREC_SIZE = struct.calcsize(_FMT_PLAYER_REC)   # 36

# String / bytes helpers

def _pack_name(s: str) -> bytes:
    """Encode name as fixed-width null-padded bytes."""
    return s.encode("utf-8")[:NAME_LEN].ljust(NAME_LEN, b"\x00")

def _unpack_name(b: bytes) -> str:
    return b.rstrip(b"\x00").decode("utf-8")

def _pack_color(hex_str: str) -> bytes:
    """'#00FFAA' -> 3 raw bytes."""
    h = hex_str.lstrip("#")
    return bytes(int(h[i : i + 2], 16) for i in (0, 2, 4))

def _unpack_color(b: bytes) -> str:
    return "#{:02X}{:02X}{:02X}".format(b[0], b[1], b[2])

# Player record (reused inside GAME_STATE and JOIN_OK)

def _pack_player(pid: int, x: float, y: float,
                 color: str, name: str, last_input_seq: int) -> bytes:
    return struct.pack(
        _FMT_PLAYER_REC,
        pid, x, y, _pack_color(color), _pack_name(name), last_input_seq,
    )

def _unpack_player(raw: bytes, offset: int) -> tuple[dict, int, int]:
    """Returns (player_dict, pid, new_offset)."""
    pid, x, y, color_b, name_b, seq = struct.unpack_from(_FMT_PLAYER_REC, raw, offset)
    p = {
        "x": x, "y": y,
        "color": _unpack_color(color_b),
        "name": _unpack_name(name_b),
        "last_input_seq": seq,
    }
    return p, pid, offset + _PREC_SIZE

# Encoders

def _enc_connect(d: dict) -> bytes:
    return struct.pack(_FMT_CONNECT, _ID["CONNECT"], _pack_name(d["name"]))

def _enc_disconnect(_: dict) -> bytes:
    return struct.pack(_FMT_DISCONNECT, _ID["DISCONNECT"])

def _enc_ack(d: dict) -> bytes:
    return struct.pack(_FMT_ACK, _ID["ACK"], d["seq"])

def _enc_input(d: dict) -> bytes:
    return struct.pack(_FMT_INPUT, _ID["INPUT"],
                       d["seq"], d["dx"], d["dy"], d["dt"])

def _enc_game_state(d: dict) -> bytes:
    players = d["players"]
    hdr = struct.pack(_FMT_GAME_HDR, _ID["GAME_STATE"], d["t"], len(players))
    records = b"".join(
        _pack_player(int(pid_str), p["x"], p["y"],
                     p["color"], p["name"], p.get("last_input_seq", 0))
        for pid_str, p in players.items()
    )
    return hdr + records

def _enc_join_ok(d: dict) -> bytes:
    existing = d.get("existing_players", {})
    hdr = struct.pack(
        _FMT_JOIN_OK_HDR,
        _ID["JOIN_OK"], d["seq"], d["pid"],
        d["x"], d["y"],
        _pack_color(d["color"]), _pack_name(d["name"]),
        d["world_w"], d["world_h"],
        len(existing),
    )
    records = b"".join(
        _pack_player(int(pid_str), p["x"], p["y"], p["color"], p["name"], 0)
        for pid_str, p in existing.items()
    )
    return hdr + records

def _enc_player_join(d: dict) -> bytes:
    return struct.pack(
        _FMT_PLAYER_JOIN,
        _ID["PLAYER_JOIN"], d["seq"], d["pid"],
        d["x"], d["y"],
        _pack_color(d["color"]), _pack_name(d["name"]),
    )

def _enc_player_quit(d: dict) -> bytes:
    return struct.pack(_FMT_PLAYER_QUIT,
                       _ID["PLAYER_QUIT"], d["seq"], d["pid"],
                       _pack_name(d["name"]))

def _enc_full(_: dict) -> bytes:
    return struct.pack("!B", _ID["FULL"])

_ENCODERS: dict = {
    "CONNECT":     _enc_connect,
    "DISCONNECT":  _enc_disconnect,
    "ACK":         _enc_ack,
    "INPUT":       _enc_input,
    "GAME_STATE":  _enc_game_state,
    "JOIN_OK":     _enc_join_ok,
    "PLAYER_JOIN": _enc_player_join,
    "PLAYER_QUIT": _enc_player_quit,
    "FULL":        _enc_full,
}

# Decoders

def _dec_connect(raw: bytes) -> dict:
    _, name_b = struct.unpack(_FMT_CONNECT, raw)
    return {"type": "CONNECT", "name": _unpack_name(name_b)}

def _dec_disconnect(_: bytes) -> dict:
    return {"type": "DISCONNECT"}

def _dec_ack(raw: bytes) -> dict:
    _, seq = struct.unpack(_FMT_ACK, raw)
    return {"type": "ACK", "seq": seq}

def _dec_input(raw: bytes) -> dict:
    _, seq, dx, dy, dt = struct.unpack(_FMT_INPUT, raw)
    return {"type": "INPUT", "seq": seq, "dx": dx, "dy": dy, "dt": dt}

def _dec_game_state(raw: bytes) -> dict:
    hdr_size = struct.calcsize(_FMT_GAME_HDR)
    _, t, num_players = struct.unpack_from(_FMT_GAME_HDR, raw, 0)
    players: dict = {}
    offset = hdr_size
    for _ in range(num_players):
        p, pid, offset = _unpack_player(raw, offset)
        players[str(pid)] = p
    return {"type": "GAME_STATE", "t": t, "players": players}

def _dec_join_ok(raw: bytes) -> dict:
    hdr_size = struct.calcsize(_FMT_JOIN_OK_HDR)
    (_, seq, pid, x, y,
     color_b, name_b, ww, wh, num_existing) = struct.unpack_from(_FMT_JOIN_OK_HDR, raw, 0)
    existing: dict = {}
    offset = hdr_size
    for _ in range(num_existing):
        p, epid, offset = _unpack_player(raw, offset)
        existing[str(epid)] = p
    return {
        "type": "JOIN_OK", "seq": seq, "pid": pid,
        "x": x, "y": y,
        "color": _unpack_color(color_b), "name": _unpack_name(name_b),
        "world_w": ww, "world_h": wh,
        "existing_players": existing,
    }

def _dec_player_join(raw: bytes) -> dict:
    _, seq, pid, x, y, color_b, name_b = struct.unpack(_FMT_PLAYER_JOIN, raw)
    return {
        "type": "PLAYER_JOIN", "seq": seq, "pid": pid,
        "x": x, "y": y,
        "color": _unpack_color(color_b), "name": _unpack_name(name_b),
    }

def _dec_player_quit(raw: bytes) -> dict:
    _, seq, pid, name_b = struct.unpack(_FMT_PLAYER_QUIT, raw)
    return {"type": "PLAYER_QUIT", "seq": seq, "pid": pid,
            "name": _unpack_name(name_b)}

def _dec_full(_: bytes) -> dict:
    return {"type": "FULL"}

_DECODERS: dict[int, object] = {
    0x01: _dec_connect,
    0x02: _dec_disconnect,
    0x03: _dec_ack,
    0x04: _dec_join_ok,
    0x05: _dec_game_state,
    0x06: _dec_input,
    0x07: _dec_player_join,
    0x08: _dec_player_quit,
    0x09: _dec_full,
}

# Public API

def encode(data: dict) -> bytes:
    """Serialize a packet dict to binary. Raises ValueError on unknown type."""
    ptype = data.get("type", "")
    encoder = _ENCODERS.get(ptype)
    if encoder is None:
        raise ValueError(f"Unknown packet type: {ptype!r}")
    return encoder(data)  # type: ignore[operator]


def decode(raw: bytes) -> dict:
    """Deserialize a binary packet to a dict. Raises ValueError on bad data."""
    if not raw:
        raise ValueError("Empty packet")
    ptype_id = raw[0]
    decoder = _DECODERS.get(ptype_id)
    if decoder is None:
        raise ValueError(f"Unknown packet type ID: {ptype_id:#04x}")
    return decoder(raw)  # type: ignore[operator]