"""This seat's OWN behavior profile — the mirror image of ``rival_profile.py``.

IMPROVEMENT_STRATEGIES_PHASE2.md §3. ``rival_profile.py`` builds a model of
a rival's habits (preferred quadrant, redsign-contest rate) from public
signals. A strong opponent will build the exact same model of THIS seat —
and if our own pickup-hour rhythm clusters predictably, a rival can time a
SNAP/EMP/chaff to it the same way we now reason about theirs (see
doctrine's own "avoid hour 6-9 pickups" / "space pickups >=3 hours apart"
advice, which is prose only and unenforced).

Deliberately a SEPARATE module from ``rival_profile.py`` even though the
accumulation shape is nearly identical (per-seat vs. per-self is a
different mental model, not just a different key), and deliberately narrow
for a first cut: pickup-hour distribution only. Drop-offset pattern was
also proposed in the strategy doc but pickup-hour is the sharper, more
directly weaponisable signal (a SNAP/EMP/chaff timed to a predictable
pickup hour denies the whole outing) — see ``get_profile``'s own
docstring for the follow-on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from sea_of_colours.orchestrator_2.harnesses.stark_direwolf._v7 import (
    memory as memory_mod,
)

_PROFILE_FLAG = "self_profile_pickup_hours"
#: Below this many recorded pickups, "predictable" is noise with a label —
#: mirrors rival_profile.py's own _MIN_SIGHTINGS_FOR_QUADRANT gate.
_MIN_SAMPLES_FOR_PREDICTABLE = 3


def _pickup_hours(executed_moves: Sequence[Mapping[str, Any]]) -> List[int]:
    """Hour of each ``pickup`` move — hour is 1-based position in the queue.

    Matches the engine's own hour numbering (RULEBOOK: each action takes
    exactly one hour, resolved in queue order) and the same convention
    ``last_night.py`` already reads execution logs against.
    """
    hours: List[int] = []
    for i, m in enumerate(executed_moves or []):
        if isinstance(m, Mapping) and str(m.get("a")) == "pickup":
            hours.append(i + 1)
    return hours


def record_turn(
    session_id: str,
    player: str,
    executed_moves: Sequence[Mapping[str, Any]],
    *,
    store: Optional[Any] = None,
) -> None:
    """Accumulate this turn's compiled pickup hours into the running profile.

    Call once per night from ``harness.py``, on the FINAL compiled move
    list (after the sanitizer and whitewalker's own post-processing) —
    the same read-modify-write flag pattern ``rival_profile.record_turn``
    and whitewalker's one-time flag already use.
    """
    hours = _pickup_hours(executed_moves)
    if not hours:
        return
    profile = memory_mod.get_flag(
        session_id, player, _PROFILE_FLAG, default=None, store=store,
    )
    if not isinstance(profile, dict):
        profile = {"pickup_hour_counts": {}, "sightings": 0}
    counts = dict(profile.get("pickup_hour_counts") or {})
    sightings = int(profile.get("sightings") or 0)
    for h in hours:
        key = str(h)
        counts[key] = int(counts.get(key, 0) or 0) + 1
        sightings += 1
    memory_mod.set_flag(
        session_id, player, _PROFILE_FLAG,
        {"pickup_hour_counts": counts, "sightings": sightings},
        store=store,
    )


def get_profile(
    session_id: str, player: str, *, store: Optional[Any] = None,
) -> Dict[str, Any]:
    """This seat's own accumulated pickup-hour rhythm.

    ``predictable_pickup_hour`` is ``None`` until at least
    ``_MIN_SAMPLES_FOR_PREDICTABLE`` pickups have been recorded — one or
    two nights of coincidence is not a rhythm. Once past the gate, it is
    the most-recorded hour, same "no fractional purity check" convention
    ``rival_profile.get_profile`` already uses for quadrant preference.

    Follow-on (not built here, noted for the next pass): drop-offset
    pattern (always landing adjacent-north of a probe, say) is the other
    signal the strategy doc proposed. Pickup hour is sharper because it is
    the one number a rival's weapon timing can target directly.
    """
    profile = memory_mod.get_flag(
        session_id, player, _PROFILE_FLAG, default=None, store=store,
    )
    if not isinstance(profile, dict):
        return {"sightings": 0, "predictable_pickup_hour": None}
    counts = profile.get("pickup_hour_counts") or {}
    sightings = int(profile.get("sightings") or 0)
    predictable = None
    if sightings >= _MIN_SAMPLES_FOR_PREDICTABLE and counts:
        predictable = int(max(counts, key=lambda h: counts.get(h, 0)))
    return {"sightings": sightings, "predictable_pickup_hour": predictable}


def _split_unit_blocks(moves: Sequence[Mapping[str, Any]]) -> List[tuple]:
    """Strictly-consecutive same-``unit`` runs, in order.

    A move with no ``unit`` (probe/emp_launch/chaff_flare/snap) is its own
    singleton block keyed ``None`` — never merged into a neighbour, since
    it has no owner to attribute a swap to.
    """
    blocks: List[tuple] = []
    cur_unit: Any = object()  # sentinel, never equals a real unit or None
    cur: List[Mapping[str, Any]] = []
    for m in moves or []:
        u = m.get("unit") if isinstance(m, Mapping) else None
        if u != cur_unit or u is None:
            if cur:
                blocks.append((cur_unit, cur))
            cur_unit = u
            cur = [m]
        else:
            cur.append(m)
    if cur:
        blocks.append((cur_unit, cur))
    return blocks


def maybe_jitter_pickup_hour(
    final_moves: Sequence[Mapping[str, Any]], predictable_hour: Optional[int],
) -> "tuple[List[Mapping[str, Any]], bool]":
    """Swap two whole-night harvester blocks if it moves a pickup off a
    predictable hour, for FREE (same chains, same banked value, only the
    absolute hour each lands on changes).

    Phase 3 (IMPROVEMENT_STRATEGIES_PHASE2.md §3), resolved design: enforce
    only when it costs nothing, report otherwise (this codebase's OBS-27
    convention — never silently trade away a chosen play's value). Scoped
    to the one case that is PROVABLY safe without deeper legality analysis:
    exactly two harvester blocks, no interleaved probe/weapon moves between
    or around them (a probe queued mid-night could be feeding a specific
    unit's hot-drop; splitting that dependency is exactly the kind of
    silent-cut risk OBS-27 warns about, so this backs off rather than
    guessing). Returns ``(moves, applied)`` — ``moves`` is unchanged and
    ``applied`` is ``False`` when the swap isn't safe or doesn't help.
    """
    if predictable_hour is None:
        return list(final_moves or []), False
    moves = list(final_moves or [])
    if predictable_hour not in _pickup_hours(moves):
        return moves, False
    blocks = _split_unit_blocks(moves)
    harvester_blocks = [(u, b) for u, b in blocks if u is not None]
    keyless_blocks = [(u, b) for u, b in blocks if u is None]
    if len(harvester_blocks) != 2 or keyless_blocks:
        return moves, False
    (_u1, b1), (_u2, b2) = harvester_blocks
    swapped = list(b2) + list(b1)
    if predictable_hour in _pickup_hours(swapped):
        return moves, False  # symmetric blocks — swap doesn't actually help
    return swapped, True

