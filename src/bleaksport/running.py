# bleaksport/running.py
from __future__ import annotations
import struct, time, asyncio
from dataclasses import dataclass
from typing import Callable, Optional, Awaitable, List
from bleak import BleakClient
from .core import s

# RSCS
UUID_RSCS      = s(0x1814)
UUID_RSC_MEAS  = s(0x2A53)
# CPS (power, e.g., Stryd over BLE)
UUID_CPS       = s(0x1818)
UUID_CP_MEAS   = s(0x2A63)

@dataclass
class RunningSample:
    timestamp: float
    speed_mps: float
    cadence_spm: int
    stride_length_m: Optional[float]
    total_distance_m: Optional[float]
    is_running: Optional[bool]
    power_watts: Optional[int] = None

class RunningSession:
    """
    A lightweight decoder that subscribes to RSCS (and CPS if present) on an
    already-connected BleakClient and emits RunningSample via callbacks.
    """
    CHAR_RSCS = UUID_RSC_MEAS
    CHAR_CPS  = UUID_CP_MEAS

    def __init__(self):
        self._callbacks: List[Callable[[RunningSample], Optional[Awaitable[None]]]] = []
        self._last: Optional[RunningSample] = None
        self._started = False

    def on_running(self, cb: Callable[[RunningSample], Optional[Awaitable[None]]]) -> None:
        self._callbacks.append(cb)

    async def start(self, client: BleakClient) -> None:
        """Subscribe to characteristics on an already-connected client."""
        if self._started:
            return
        # RSCS is required for running metrics
        await client.start_notify(self.CHAR_RSCS, self._handle_rsc)
        # CPS is optional (power)
        try:
            await client.start_notify(self.CHAR_CPS, self._handle_cp)
        except Exception:
            pass
        self._started = True

    async def stop(self, client: BleakClient) -> None:
        """Unsubscribe from notifications (ignores missing chars)."""
        if not self._started:
            return
        for uuid in (self.CHAR_RSCS, self.CHAR_CPS):
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass
        self._started = False

    # ---- internal emit ----
    def _emit(self, sample: RunningSample) -> None:
        self._last = sample
        async def _dispatch():
            tasks = []
            for cb in self._callbacks:
                res = cb(sample)
                if asyncio.iscoroutine(res):
                    tasks.append(asyncio.create_task(res))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.create_task(_dispatch())

    # ---- RSCS (0x2A53) ----
    def _handle_rsc(self, _h: int, data: bytearray):
        ts = time.time()
        off = 0
        flags = data[off]; off += 1
        stride_present = bool(flags & 0x01)
        dist_present   = bool(flags & 0x02)
        is_running     = bool(flags & 0x04)

        speed_raw   = struct.unpack_from("<H", data, off)[0]; off += 2
        cadence_spm = data[off]; off += 1
        speed_mps   = speed_raw / 256.0

        stride_m = None
        if stride_present:
            stride_cm = struct.unpack_from("<H", data, off)[0]; off += 2
            stride_m = stride_cm / 100.0

        total_distance_m = None
        if dist_present:
            dist_raw = struct.unpack_from("<I", data, off)[0]; off += 4
            total_distance_m = dist_raw / 10.0

        power = self._last.power_watts if self._last else None
        sample = RunningSample(
            timestamp=ts,
            speed_mps=speed_mps,
            cadence_spm=cadence_spm,
            stride_length_m=stride_m,
            total_distance_m=total_distance_m,
            is_running=is_running,
            power_watts=power,
        )
        self._emit(sample)

    # ---- CPS (0x2A63) ----
    def _handle_cp(self, _h: int, data: bytearray):
        ts = time.time()
        off = 0
        off += 2  # flags (unused)
        inst_power = struct.unpack_from("<h", data, off)[0]; off += 2

        prev = self._last
        sample = RunningSample(
            timestamp=ts,
            speed_mps=prev.speed_mps if prev else 0.0,
            cadence_spm=prev.cadence_spm if prev else 0,
            stride_length_m=prev.stride_length_m if prev else None,
            total_distance_m=prev.total_distance_m if prev else None,
            is_running=prev.is_running if prev else None,
            power_watts=int(inst_power),
        )
        self._emit(sample)
