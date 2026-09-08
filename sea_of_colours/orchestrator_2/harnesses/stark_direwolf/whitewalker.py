"""whitewalker EMP — the seat's scripted, one-time opening strike.

This is not a menu option the model chooses between (see the module
docstring in ``docs/TEACHING_WEAPONS.md``: a bare EMP salvo shows +0
immediate yield, and a model asked to pick the highest-scoring option
essentially never picks it). Instead this is deterministic, harness-forced
behaviour, applied once per game:

* **Buy** — ``orbit_policy.py`` priority 0 buys the one EMP this spends,
  ahead of repairs/fleet/probes, the moment blue and credits allow it.
* **Fire** — the first night a real rival probe is visible (probe
  launches are public, RULEBOOK §0.4, but simultaneous — there is no
  rival probe history on literal night 1), fire at the freshest rival
  probes, up to the missiles-per-launch cap.
* **Fallback** — on any night before a real target exists, there is
  nothing worth denying yet. Instead of firing, deterministically steer
  that night's own probe placement toward whichever candidate destination
  reveals the most NEW fog, rather than the harness's default pick.

Both the fire decision and the fallback are one-shot per game: once the
EMP is spent, this module has nothing further to do (see ``harness.py``
and the ``whitewalker_emp_fired`` flag in ``_v7/memory.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sea_of_colours.game.tuning import probe_vision_radius
from sea_of_colours.orchestrator_2.harnesses.stark_direwolf._v7 import (
    memory as memory_mod,
)
from sea_of_colours.orchestrator_2.harnesses.stark_direwolf.supersede import (
    _enemy_probes,
)
from sea_of_colours.orchestrator_2.harnesses.stark_direwolf.world_view import (
    _my_probe_cells,
)

Cell = Tuple[int, int]

#: Memory flag name — set once the salvo is actually fired (distinct
#: from ``whitewalker_emp_committed`` in orbit_policy/orbit.py, which
#: marks the *purchase*). Firing can lag the purchase by several nights
#: if no rival probe is visible yet.
_FIRED_FLAG = "whitewalker_emp_fired"


def stock(agent_view: Mapping[str, Any]) -> int:
    """Own EMP count — Rung 1: the night phase has to be able to see this."""
    ws = (agent_view.get("orbit") or {}).get("weapon_stock") or {}
    try:
        return int(ws.get("emp", 0) or 0)
    except (TypeError, ValueError):
        return 0


def missiles_per_launch(agent_view: Mapping[str, Any], default: int = 3) -> int:
    """Missile count for one EMP charge, read from the view when present."""
    spec = ((agent_view.get("orbit") or {}).get("weapon_specs") or {}).get(
        "emp"
    ) or {}
    try:
        return int(spec.get("missiles_per_launch", default) or default)
    except (TypeError, ValueError):
        return default


def already_fired(
    session_id: str, player: str, *, store: Optional[Any] = None,
) -> bool:
    """Has the scripted strike already fired this game?"""
    return bool(memory_mod.get_flag(session_id, player, _FIRED_FLAG, store=store))


def mark_fired(
    session_id: str, player: str, *, store: Optional[Any] = None,
) -> None:
    """Record that the strike has fired — never fires a second time."""
    memory_mod.set_flag(session_id, player, _FIRED_FLAG, True, store=store)


# ── rival probe targeting ────────────────────────────────────────────


def rival_probe_targets(
    agent_view: Mapping[str, Any], *, missiles: int,
) -> Tuple[List[Cell], List[str]]:
    """Freshest rival probes to hit, up to ``missiles`` cells.

    Reuses :func:`supersede._enemy_probes` — the same public-sighting
    channel (``competitor_intel``) the supersede menu already ranks by
    recency — rather than a second intel path. Empty when no rival probe
    is currently visible, which is expected on night 1 (probe launches
    are public but simultaneous: you cannot see night N's rival probes
    while planning night N).
    """
    probes = _enemy_probes(agent_view)
    if not probes:
        return [], ["no rival probe currently visible — nothing to target"]
    picked = [tuple(p["at"]) for p in probes[:missiles]]
    notes = [
        f"{len(picked)} rival probe(s) targeted, freshest first "
        f"(last seen day {probes[0].get('day_seen')})"
    ]
    return picked, notes


# ── fallback: least-overlap probe destination ────────────────────────


def _euclid_disk(
    centre: Cell, radius: int, bounds: Optional[Tuple[int, int]] = None,
) -> set:
    """Every cell a probe at ``centre`` can see — Euclidean disk (§ canonical
    config: ``dx²+dy²≤radius²``), matching probe vision, NOT the EMP's
    Manhattan diamond.
    """
    cx, cy = centre
    r2 = radius * radius
    out: set = set()
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if dx * dx + dy * dy > r2:
                continue
            x, y = cx + dx, cy + dy
            if x < 0 or y < 0:
                continue
            if bounds is not None and (x >= bounds[0] or y >= bounds[1]):
                continue
            out.add((x, y))
    return out


def _bounds(agent_view: Mapping[str, Any]) -> Optional[Tuple[int, int]]:
    meta = agent_view.get("meta") or {}
    hud = agent_view.get("hud") or {}
    w = meta.get("width") or hud.get("width")
    h = meta.get("height") or hud.get("height")
    try:
        return (int(w), int(h))
    except (TypeError, ValueError):
        return None


def _cell(at: Any) -> Optional[Cell]:
    if isinstance(at, Mapping):
        at = (at.get("x"), at.get("y"))
    if not isinstance(at, (list, tuple)) or len(at) < 2:
        return None
    try:
        return (int(at[0]), int(at[1]))
    except (TypeError, ValueError):
        return None


def least_overlap_probe(
    probe_hints: Sequence[Mapping[str, Any]], agent_view: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    """Which candidate probe destination reveals the most NEW fog.

    "New" = cells inside the candidate's vision disk that are not already
    covered by one of the seat's own live probes. Ties broken by the
    harness's own ranking (first candidate wins), so this only overrides
    the default pick when there is a real, unambiguous gain.

    Returns ``None`` when there are no candidates to choose between.
    """
    candidates = [h for h in probe_hints if _cell(h.get("at")) is not None]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    radius = probe_vision_radius()
    bounds = _bounds(agent_view)
    already_covered: set = set()
    for c in _my_probe_cells(agent_view):
        already_covered |= _euclid_disk(c, radius, bounds)

    best, best_new = None, -1
    for h in candidates:
        cell = _cell(h.get("at"))
        disk = _euclid_disk(cell, radius, bounds)
        new_cells = len(disk - already_covered)
        if new_cells > best_new:
            best, best_new = h, new_cells
    return best
