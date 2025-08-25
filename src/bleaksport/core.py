from __future__ import annotations

_BASE = "-0000-1000-8000-00805f9b34fb"


def s(uuid16: int) -> str:
    """Build a full 128-bit UUID from a 16-bit SIG number."""
    return f"0000{uuid16:04x}{_BASE}"
