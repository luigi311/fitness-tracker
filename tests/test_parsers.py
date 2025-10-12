# tests/test_parsers.py
from __future__ import annotations

import math
from itertools import combinations
from pathlib import Path

import pytest
from fitness_tracker.workouts import load_workout, normalize_workout

HERE = Path(__file__).parent
DATA = HERE / "data"

SUPPORTED = {".json", ".fit"}


# -------- discovery --------
def discover_pairs():
    """
    Find all stems that exist in 2+ supported formats, and yield JSON↔other pairs.
    e.g., ("Aerobic_Speed_Endurance.json", "Aerobic_Speed_Endurance.fit").
    """
    by_stem: dict[str, list[Path]] = {}
    for p in DATA.glob("*"):
        if p.suffix.lower() in SUPPORTED and p.is_file():
            by_stem.setdefault(p.stem, []).append(p)

    pairs: list[tuple[Path, Path]] = []
    for files in by_stem.values():
        # prefer JSON↔other comparisons
        jsons = [p for p in files if p.suffix.lower() == ".json"]
        others = [p for p in files if p.suffix.lower() != ".json"]
        if jsons and others:
            for j in jsons:
                for o in others:
                    pairs.append((j, o))
        # if no JSON present but >=2 formats exist, compare all combos
        elif len(files) >= 2:
            pairs.extend(combinations(files, 2))
    return pairs


PAIRS = discover_pairs()
if not PAIRS:
    # make it obvious in CI if nothing was discovered
    msg = f"No comparable file pairs found in {DATA}"
    raise SystemExit(msg)

# FTPs to test; adjust as you like
FTPS = [150, 200, 250]


# -------- helpers --------
def _approx(a: float | None, b: float | None, tol: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return math.isclose(float(a), float(b), rel_tol=0, abs_tol=tol)


def _compare_normalized(n_a: dict, n_b: dict) -> None:
    sa, sb = n_a["steps"], n_b["steps"]
    assert len(sa) == len(sb), f"Different step counts: {len(sa)} vs {len(sb)}"

    for i, (ea, eb) in enumerate(zip(sa, sb, strict=False)):
        da, db = float(ea["duration_s"]), float(eb["duration_s"])
        assert _approx(da, db, 0.5), f"Step {i} duration mismatch: {da} vs {db}"

        ka, kb = ea["kind"], eb["kind"]
        assert ka == kb, f"Step {i} kind mismatch: {ka} vs {kb}"

        if ka == "power":
            pa, pb = ea["power"], eb["power"]
            assert _approx(pa["mid"], pb["mid"], 1.0), (
                f"Step {i} power mid: {pa['mid']} vs {pb['mid']}"
            )
            # Compare lo/hi only if both present
            if (pa["lo"] is not None) and (pb["lo"] is not None):
                assert _approx(pa["lo"], pb["lo"], 1.0), (
                    f"Step {i} power lo: {pa['lo']} vs {pb['lo']}"
                )
            if (pa["hi"] is not None) and (pb["hi"] is not None):
                assert _approx(pa["hi"], pb["hi"], 1.0), (
                    f"Step {i} power hi: {pa['hi']} vs {pb['hi']}"
                )
        elif ka == "pace":
            va, vb = ea["pace"], eb["pace"]
            assert _approx(va["mid_mps"], vb["mid_mps"], 0.01), (
                f"Step {i} pace mid: {va['mid_mps']} vs {vb['mid_mps']}"
            )
            if (va["lo_mps"] is not None) and (vb["lo_mps"] is not None):
                assert _approx(va["lo_mps"], vb["lo_mps"], 0.01), (
                    f"Step {i} pace lo: {va['lo_mps']} vs {vb['lo_mps']}"
                )
            if (va["hi_mps"] is not None) and (vb["hi_mps"] is not None):
                assert _approx(va["hi_mps"], vb["hi_mps"], 0.01), (
                    f"Step {i} pace hi: {va['hi_mps']} vs {vb['hi_mps']}"
                )


# -------- tests --------
@pytest.mark.parametrize(
    ("json_path", "other_path"), PAIRS, ids=lambda p: p.name if isinstance(p, Path) else str(p),
)
@pytest.mark.parametrize("ftp", FTPS)
def test_json_equivalence_against_other_formats(
    json_path: Path, other_path: Path, ftp: int
) -> None:
    """
    For every discovered pair, ensure the normalized structures are equivalent.
    If the pair includes JSON, it acts as the 'golden' reference; otherwise we still
    compare both directions (e.g., FIT).
    """
    # Load
    w_a = load_workout(json_path)
    w_b = load_workout(other_path)

    n_a = normalize_workout(w_a, ftp_watts=ftp)
    n_b = normalize_workout(w_b, ftp_watts=ftp)

    # Sanity checks
    assert len(n_a["steps"]) > 0, f"{json_path.name} yielded no steps"
    assert len(n_b["steps"]) > 0, f"{other_path.name} yielded no steps"

    # Compare
    _compare_normalized(n_a, n_b)
