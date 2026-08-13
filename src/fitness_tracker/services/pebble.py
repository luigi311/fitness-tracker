"""Pebble bridge service facade for UI composition."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fitness_tracker.core.guidance import StepGuidance, TargetDomain

if TYPE_CHECKING:
    from pebble_bridge import PebbleBridge


def apply_pebble_guidance(
    guidance: StepGuidance,
    *,
    bridge: PebbleBridge | None,
) -> None:
    """Publish the active target band, or clear it for an untargeted step."""
    if bridge is None:
        return
    if guidance.domain is TargetDomain.NONE:
        bridge.update(tgt_kind=guidance.pebble_kind)
        return
    bridge.update(
        tgt_kind=guidance.pebble_kind,
        tgt_lo=guidance.low,
        tgt_hi=guidance.high,
    )
