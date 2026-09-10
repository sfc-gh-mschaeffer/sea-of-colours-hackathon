"""Per-opponent behavior profile — the first POSITIVE memory in this harness.

IMPROVEMENT_STRATEGIES.md §5. Every durable memory that exists elsewhere in
this fork (``hazard_memory.py``, ``frontier.py``'s enemy-landing avoidance,
``option_economics.py``'s pure-survival decay) is a negative constraint:
"don't go here", "discount this stale echo", "this seam is probably spent".
Nothing accumulates a POSITIVE model of a rival — where they tend to work,
whether they contest redsigns, how often they've been seen at all. The raw
signal (public probe launches, which redsigns a rival's probe sat on) is
already flowing through the harness for other purposes (``supersede.py``);
this module is the first thing that keeps a running tally of it per seat
rather than reading it fresh and throwing it away every night.

Deliberately narrow for a first cut: quadrant preference and redsign-contest
rate, both built from data this harness already computes elsewhere. Skips
weapon-firing-hour timing (also proposed in the strategy doc) — that needs a
new per-hour event feed this pass didn't have time to wire up cleanly;
noted as a follow-on rather than guessed at.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from sea_of_colours.orchestrator_2.harnesses.stark_direwolf._v7 import (
    memory as memory_mod,
)
from sea_of_colours.orchestrator_2.harnesses.stark_direwolf.supersede import (
    _enemy_probes,
    _redsign_finder_cells,
)

_QUADRANTS = ("NW", "NE", "SW", "SE")


def _quadrant(
    cell: Any, width: int, height: int,
) -> Optional[str]:
    if not (isinstance(cell, (list, tuple)) and len(cell) == 2):
        return None
    try:
        x, y = int(cell[0]), int(cell[1])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    north = y < height / 2
    west = x < width / 2
    if north and west:
        return "NW"
    if north and not west:
        return "NE"
    if not north and west:
        return "SW"
    return "SE"


def _profile_flag(rival_seat: str) -> str:
    return f"rival_profile_{rival_seat}"


def _grid_dims(agent_view: Mapping[str, Any]) -> tuple:
    meta = agent_view.get("meta") or {}
    hud = agent_view.get("hud") or {}
    w = meta.get("width") or hud.get("width") or 0
    h = meta.get("height") or hud.get("height") or 0
    try:
        return int(w), int(h)
    except (TypeError, ValueError):
        return 0, 0


def record_turn(
    session_id: str, player: str, agent_view: Mapping[str, Any],
    *, store: Optional[Any] = None,
) -> None:
    """Accumulate this turn's public sightings into each rival's profile.

    Call once per night, after the view is available. Read-modify-write
    over the existing ``_v7/memory.py`` flag store (the same mechanism
    whitewalker's one-time flag uses) — one flag per rival seat, so a
    2-seat game touches one flag, a 4-seat game touches three, and
    nothing here needs a new storage primitive.
    """
    width, height = _grid_dims(agent_view)
    finders = _redsign_finder_cells(agent_view)
    probes = _enemy_probes(agent_view)
    if not probes:
        return

    by_seat: Dict[str, list] = {}
    for row in probes:
        owner = row.get("owner")
        if not owner:
            continue
        by_seat.setdefault(str(owner), []).append(row)

    for seat, rows in by_seat.items():
        profile = memory_mod.get_flag(
            session_id, player, _profile_flag(seat), default=None, store=store,
        )
        if not isinstance(profile, dict):
            profile = {"quadrant_counts": {q: 0 for q in _QUADRANTS},
                       "sightings": 0, "redsign_finder_sightings": 0}
        quadrant_counts = dict(profile.get("quadrant_counts") or {})
        for q in _QUADRANTS:
            quadrant_counts.setdefault(q, 0)
        sightings = int(profile.get("sightings") or 0)
        finder_sightings = int(profile.get("redsign_finder_sightings") or 0)

        for row in rows:
            cell = row.get("at")
            q = _quadrant(cell, width, height)
            if q is not None:
                quadrant_counts[q] = quadrant_counts.get(q, 0) + 1
            sightings += 1
            if isinstance(cell, (list, tuple)) and tuple(cell) in finders:
                finder_sightings += 1

        memory_mod.set_flag(
            session_id, player, _profile_flag(seat),
            {
                "quadrant_counts": quadrant_counts,
                "sightings": sightings,
                "redsign_finder_sightings": finder_sightings,
            },
            store=store,
        )


def get_profile(
    session_id: str, player: str, rival_seat: str, *, store: Optional[Any] = None,
) -> Dict[str, Any]:
    """This seat's accumulated read on ``rival_seat``, or an empty profile.

    ``preferred_quadrant`` is ``None`` until at least a few sightings exist
    — a guess off one probe is not a profile, it's noise with a label.
    """
    profile = memory_mod.get_flag(
        session_id, player, _profile_flag(rival_seat), default=None, store=store,
    )
    if not isinstance(profile, dict):
        return {
            "sightings": 0, "preferred_quadrant": None,
            "redsign_contest_rate": None,
        }
    counts = profile.get("quadrant_counts") or {}
    sightings = int(profile.get("sightings") or 0)
    preferred = None
    _MIN_SIGHTINGS_FOR_QUADRANT = 3
    if sightings >= _MIN_SIGHTINGS_FOR_QUADRANT and counts:
        preferred = max(counts, key=lambda q: counts.get(q, 0))
    finder_sightings = int(profile.get("redsign_finder_sightings") or 0)
    contest_rate = (finder_sightings / sightings) if sightings else None
    return {
        "sightings": sightings,
        "preferred_quadrant": preferred,
        "redsign_contest_rate": (
            round(contest_rate, 2) if contest_rate is not None else None
        ),
    }
