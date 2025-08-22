# bleaksport/discover.py
from __future__ import annotations
from typing import Optional, List
from bleak import BleakScanner
from .core import s

UUID_RSCS = s(0x1814)
UUID_CSCS = s(0x1816)
UUID_CPS  = s(0x1818)

async def discover_speed_cadence_devices(timeout: float = 5.0, name_contains: Optional[str] = None):
    devices = await BleakScanner.discover(timeout=timeout, service_uuids=[UUID_RSCS, UUID_CSCS])
    return _filter_by_name(devices, name_contains)

async def discover_running_devices(timeout: float = 5.0, name_contains: Optional[str] = None):
    devices = await BleakScanner.discover(timeout=timeout, service_uuids=[UUID_RSCS])
    return _filter_by_name(devices, name_contains)

async def discover_cycling_devices(timeout: float = 5.0, name_contains: Optional[str] = None):
    devices = await BleakScanner.discover(timeout=timeout, service_uuids=[UUID_CSCS])
    return _filter_by_name(devices, name_contains)

async def discover_power_devices(timeout: float = 5.0, name_contains: Optional[str] = None):
    """Find devices advertising CPS (e.g., bike power meters or Stryd pods exposing power)."""
    devices = await BleakScanner.discover(timeout=timeout, service_uuids=[UUID_CPS])
    return _filter_by_name(devices, name_contains)

def _filter_by_name(devices, name_contains: Optional[str]):
    if not name_contains: return devices
    needle = name_contains.lower()
    return [d for d in devices if d.name and needle in d.name.lower()]
