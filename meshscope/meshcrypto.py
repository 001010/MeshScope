"""Meshtastic channel encryption: PSK expansion, channel hash and AES-CTR (as the firmware does it)."""

from __future__ import annotations

import base64
import binascii
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# The well-known key behind PSK index 1 ("AQ==")
DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")

# LoRa modem preset enum name -> channel name the firmware uses when a channel name is empty
PRESET_NAMES = {
    "LONG_FAST": "LongFast", "LONG_SLOW": "LongSlow", "VERY_LONG_SLOW": "VeryLongSlow",
    "MEDIUM_SLOW": "MediumSlow", "MEDIUM_FAST": "MediumFast", "SHORT_SLOW": "ShortSlow",
    "SHORT_FAST": "ShortFast", "LONG_MODERATE": "LongModerate", "SHORT_TURBO": "ShortTurbo",
    "LONG_TURBO": "LongTurbo",
}


def expand_psk(psk: bytes) -> bytes:
    """Turn a channel PSK as stored in config into the actual AES key (b"" = no encryption)."""
    if not psk:
        return b""
    if len(psk) == 1:
        idx = psk[0]
        if idx == 0:
            return b""
        key = bytearray(DEFAULT_KEY)
        key[-1] = (key[-1] + idx - 1) & 0xFF
        return bytes(key)
    if len(psk) < 16:
        return psk.ljust(16, b"\0")
    if len(psk) < 32:
        return psk.ljust(32, b"\0") if len(psk) > 16 else psk
    return psk[:32]


def parse_psk(text: str) -> bytes:
    """Accept base64 (as shown by the apps), hex, or the words default/none."""
    t = (text or "").strip()
    if t.lower() in ("", "none"):
        return b""
    if t.lower() in ("default", "aq=="):
        return b"\x01"
    if t.lower().startswith("0x"):
        return binascii.unhexlify(t[2:])
    try:
        return base64.b64decode(t, validate=True)
    except (binascii.Error, ValueError):
        return binascii.unhexlify(t)


def channel_hash(name: str, key: bytes) -> int:
    h = 0
    for b in name.encode("utf-8"):
        h ^= b
    for b in key:
        h ^= b
    return h & 0xFF


def _nonce(packet_id: int, from_node: int) -> bytes:
    return struct.pack("<QII", packet_id & 0xFFFFFFFF, from_node & 0xFFFFFFFF, 0)


def crypt(key: bytes, packet_id: int, from_node: int, data: bytes) -> bytes:
    """AES-CTR is symmetric: the same call encrypts and decrypts."""
    if not key:
        return data
    c = Cipher(algorithms.AES(key), modes.CTR(_nonce(packet_id, from_node))).encryptor()
    return c.update(data) + c.finalize()


def preset_channel_name(preset_enum_name: str) -> str:
    return PRESET_NAMES.get(preset_enum_name, preset_enum_name.title().replace("_", ""))
