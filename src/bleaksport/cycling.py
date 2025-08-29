# bleaksport/cycling.py
from __future__ import annotations
import struct, time, asyncio
from dataclasses import dataclass
from typing import Callable, Optional, Awaitable, List
from bleak import BleakClient
from .core import s

# Services / Characteristics
UUID_CSCS      = s(0x1816)
UUID_CSC_MEAS  = s(0x2A5B)
UUID_CPS       = s(0x1818)
UUID_CP_MEAS   = s(0x2A63)

@dataclass
class CyclingSample:
    timestamp: float
    cum_wheel_revs: Optional[int]
    last_wheel_event_time_s: Optional[float]
    cum_crank_revs: Optional[int]
    last_crank_event_time_s: Optional[float]
    power_watts: Optional[int] = None
    speed_mps: Optional[float] = None
    wheel_rpm: Optional[float] = None
    cadence_rpm: Optional[float] = None

class CyclingSession:
    """
    Decoder that subscribes to CSCS (+ CPS if present) on an already-connected client.
    Emits fused CyclingSample via callbacks.
    """
    CHAR_CSCS = UUID_CSC_MEAS
    CHAR_CPS  = UUID_CP_MEAS

    def __init__(self, *, wheel_circumference_m: Optional[float] = None):
        self._callbacks: List[Callable[[CyclingSample], Optional[Awaitable[None]]]] = []
        self._wheel_prev = None   # (cum:uint32, t_s)
        self._crank_prev = None   # (cum:uint16, t_s)
        self._last: Optional[CyclingSample] = None
        self._started = False
        self.wheel_circumference_m = wheel_circumference_m

    def on_cycling(self, cb: Callable[[CyclingSample], Optional[Awaitable[None]]]) -> None:
        self._callbacks.append(cb)

    async def start(self, client: BleakClient) -> None:
        if self._started:
            return
        await client.start_notify(self.CHAR_CSCS, self._handle_csc)
        try:
            await client.start_notify(self.CHAR_CPS, self._handle_cp)
        except Exception:
            pass
        self._started = True

    async def stop(self, client: BleakClient) -> None:
        if not self._started:
            return
        for uuid in (self.CHAR_CSCS, self.CHAR_CPS):
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass
        self._started = False

    def _emit(self, sample: CyclingSample) -> None:
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

    # ---- CSCS handler ----
    def _handle_csc(self, _h: int, data: bytearray):
        ts = time.time()
        off = 0
        flags = data[off]; off += 1
        wheel_present = bool(flags & 0x01)
        crank_present = bool(flags & 0x02)

        cum_wheel = last_wheel_s = None
        if wheel_present:
            cum_wheel = struct.unpack_from("<I", data, off)[0]; off += 4
            last_wheel_evt_1024 = struct.unpack_from("<H", data, off)[0]; off += 2
            last_wheel_s = last_wheel_evt_1024 / 1024.0

        cum_crank = last_crank_s = None
        if crank_present:
            cum_crank = struct.unpack_from("<H", data, off)[0]; off += 2
            last_crank_evt_1024 = struct.unpack_from("<H", data, off)[0]; off += 2
            last_crank_s = last_crank_evt_1024 / 1024.0

        power = self._last.power_watts if self._last else None
        sample = CyclingSample(
            timestamp=ts,
            cum_wheel_revs=cum_wheel, last_wheel_event_time_s=last_wheel_s,
            cum_crank_revs=cum_crank, last_crank_event_time_s=last_crank_s,
            power_watts=power,
        )

        # Derived (speed / rpm)
        if wheel_present and self.wheel_circumference_m is not None and last_wheel_s is not None:
            if self._wheel_prev is not None:
                prev_revs, prev_t = self._wheel_prev
                d_revs = (cum_wheel - prev_revs) & 0xFFFFFFFF
                dt = (last_wheel_s - prev_t) % 64.0
                if dt > 0:
                    sample.speed_mps = (d_revs * self.wheel_circumference_m) / dt
                    sample.wheel_rpm = (d_revs / dt) * 60.0
            self._wheel_prev = (cum_wheel, last_wheel_s)

        if crank_present and last_crank_s is not None:
            if self._crank_prev is not None:
                prev_revs, prev_t = self._crank_prev
                d_revs = (cum_crank - prev_revs) & 0xFFFF
                dt = (last_crank_s - prev_t) % 64.0
                if dt > 0:
                    sample.cadence_rpm = (d_revs / dt) * 60.0
            self._crank_prev = (cum_crank, last_crank_s)

        self._emit(sample)

    # ---- CPS handler ----
    def _handle_cp(self, _h: int, data: bytearray):
        ts = time.time()
        off = 0
        off += 2  # flags
        inst_power = struct.unpack_from("<h", data, off)[0]; off += 2

        prev = self._last
        sample = CyclingSample(
            timestamp=ts,
            cum_wheel_revs=prev.cum_wheel_revs if prev else None,
            last_wheel_event_time_s=prev.last_wheel_event_time_s if prev else None,
            cum_crank_revs=prev.cum_crank_revs if prev else None,
            last_crank_event_time_s=prev.last_crank_event_time_s if prev else None,
            power_watts=int(inst_power),
            speed_mps=prev.speed_mps if prev else None,
            wheel_rpm=prev.wheel_rpm if prev else None,
            cadence_rpm=prev.cadence_rpm if prev else None,
        )
        self._emit(sample)
