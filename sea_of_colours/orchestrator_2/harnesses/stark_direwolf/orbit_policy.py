"""The seat's buying policy — what to spend credits and BLUE on, in orbit.

**This file is yours to change.** It was forked out of the shared
``sea_of_colours.agent.heuristic_agent`` planner in v1.40 for one reason:
a fork could not previously edit how its agent spends money. The planner
lived in a module every seat in the room shares, so "buy weapons earlier"
was not a change any single team could make or push.

Everything below is now local. Two surfaces, deliberately separated:

* :class:`OrbitDials` — the numbers. Thresholds, stockpile caps, the
  fallback prices. Retuning the seat's economy should be an edit here and
  nowhere else, which is what makes it a safe change to hand to a coding
  assistant.
* :func:`plan_orbit_actions` — the priority order. Repair, then fleet,
  then weapons, then probes. Reordering these, or adding a priority, is
  the structural change; the dials are the cheap one.

At the shipped dials this is byte-identical to the shared planner — same
actions, same rationale string — so V12's baseline scores did not move
when it was forked. ``tests/test_orbit_policy.py`` pins that with a
differential test against the shared implementation; if you retune the
dials, that test is *expected* to fail and should be updated or dropped.

Known holes, left deliberately (they are the exercise):

* **Nothing here reads the board.** Buying is a function of credits,
  BLUE and fleet state only. A policy of the form "buy an EMP when a
  redsign is live" needs the night-phase view, which is on ``view`` and
  simply not consulted yet.
* **EMP is capped at a small stockpile** so the seat cannot hoard salvos
  it never fires. That cap is also why V12 buys weapons and leaves them
  in the rack — raising it without teaching the night phase to fire them
  makes the agent worse, not better.
"""

from __future__ import annotations

import random as _random
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple

from sea_of_colours.orchestrator_2.harnesses.stark_direwolf import supersede as _supersede
from sea_of_colours.orchestrator_2.harnesses.stark_direwolf import (
    option_economics as _option_economics,
)
from sea_of_colours.orchestrator_2.harnesses.stark_direwolf._v7 import (
    opponent_weapons as _opponent_weapons,
)

# Phase 5 (IMPROVEMENT_STRATEGIES_PHASE2.md §5) — arms-race EV calibration.
# MEASURED, not invented: total RED banked by stark_direwolf across the 20
# real validation seasons (val_vs_v12_s101..s120, file backend, alternating
# seats — see scripts/_val_run_seasons.sh), divided by nights played (7
# each), then divided by an assumed average active fleet of 2.5 harvesters
# (accounts for the day <=4 single-harvester recovery window this fork's
# own orbit.py R4 gate targets — RULEBOOK's design target is a full 3-
# harvester fleet most nights, so 2.5 is a conservative average, not the
# cap). Raw measurement: mean 376.2 red-per-night-per-seat, n=20 seasons
# (min 42.3, max 827.1 — high variance, dominated by map RNG same as the
# whole-season score spread already documented in this fork's validation
# notes). 376.2 / 2.5 ≈ 150.
#
# This is a genuinely rougher calibration than option_economics.py's own
# halo constants (measured directly off 1500+ per-cell samples across 28
# offline seasons) — flagged here rather than dressed up as more precise
# than it is. Retune once a similar per-cell sample exists for harvester
# throughput specifically.
_CALIBRATED_RED_PER_HARVESTER_PER_NIGHT = 150.0


@dataclass(frozen=True)
class OrbitDials:
    """Every number the buying policy consults.

    Prices are *fallbacks only*. The engine sends real prices on the view
    (``orbit.ship_prices`` / ``orbit.weapon_prices``) and those win; these
    exist so a stripped-down test view still plans sanely.

    The thresholds are the interesting ones — they are the seat's
    economic doctrine expressed as three numbers.
    """

    #: Probe magazine the playbook tops up toward each turn. Sized for a
    #: night of hot-drops (one securing probe per uncovered vein,
    #: RULEBOOK §3.9.7) plus an exploration probe.
    probe_target_stock: int = 4

    #: BLUE above which a weapon is ALWAYS built: chaff first when the
    #: rack is empty (the hour 5-7 / 11-13 egress jam, RULEBOOK §5),
    #: else an EMP.
    blue_always_build: int = 300
    #: BLUE above which an EMP build is rolled for at
    #: :attr:`emp_roll_chance`. Between this and
    #: :attr:`blue_always_build` the build stays a coin flip.
    blue_emp_roll: int = 250
    #: Probability of the roll above landing.
    emp_roll_chance: float = 0.5

    #: Stop buying EMP at this many in stock. Deliberately low — see the
    #: module docstring on hoarding.
    emp_stockpile_cap: int = 2
    #: Stop buying chaff at this many in stock, in the always-build band.
    chaff_stockpile_cap: int = 1
    #: BLUE above which a SNAP is bought opportunistically. Deliberately
    #: lower than :attr:`blue_emp_roll` / :attr:`blue_always_build` — SNAP
    #: is the cheapest weapon on the ladder (100 blue) and its value
    #: (denying a rival's finder probe or a same-hour landing, RULEBOOK
    #: §4.9.4) doesn't need a blue surplus to be worth having; a small
    #: buffer above its own cost is enough. See IMPROVEMENT_STRATEGIES.md
    #: §1.2 — V12 never bought SNAP at all.
    blue_snap_buy: int = 150
    #: Stop buying SNAP at this many in stock.
    snap_stockpile_cap: int = 2

    # Phase 2 (IMPROVEMENT_STRATEGIES_PHASE2.md §1) — board-aware buying.
    # Multipliers, not new absolute thresholds: applied to blue_snap_buy /
    # blue_always_build / blue_emp_roll depending on whether tonight's
    # board actually justifies spending now. 1.0 recovers today's flat
    # behavior exactly (the differential test against the shared planner
    # pins this).
    #: Multiplier on blue_snap_buy when a live redsign's finder probe is
    #: on the board RIGHT NOW — SNAP's whole value is denying it tonight,
    #: so the surplus buffer above its own cost matters less.
    urgent_snap_multiplier: float = 0.4
    #: Multiplier on blue_always_build / blue_emp_roll when a redsign is
    #: live and this seat holds no EMP/chaff yet, OR a rival's public
    #: total just rose (a threat is imminent) — denial value just rose,
    #: so the surplus bar drops. Deliberately one-directional (only ever
    #: LOWERS the bar): the differential test's synthetic sweep never
    #: populates ``redsign``/reload-history, so an "always raise on a
    #: quiet board" default would silently diverge from the pinned
    #: shared-planner baseline across the whole sweep, not just on a
    #: genuinely quiet real board. Lowering is safe because it only ever
    #: fires on a POSITIVE signal the stripped sweep never produces.
    urgent_weapon_multiplier: float = 0.6
    #: Nights remaining (inclusive of tonight) at or below which the
    #: weapon-buying tier is skipped outright — a charge bought with one
    #: night left to fire it is one more misfire away from pure waste.
    final_night_weapon_cutoff: int = 1

    # Fallback prices — mirrors of the engine constants.
    repair_cost: int = 500
    probe_build_cost: int = 250
    harvester_build_cost: int = 1500
    harvester_cap: int = 3
    emp_blue_cost: int = 200
    #: v1.45 fix — this used to default to 0, but the engine charges 250
    #: (``EMP_COST_CREDITS`` in ``game/weapons.py``). Real prices come
    #: from ``orbit.weapon_prices`` and win regardless; this fallback
    #: only matters on a stripped-down test view.
    emp_credit_cost: int = 250
    chaff_blue_cost: int = 300  # v1.36 — was 255
    chaff_credit_cost: int = 0
    snap_blue_cost: int = 100
    snap_credit_cost: int = 250
    #: Fallback for ``meta.rules.weapon_blue_cap`` (RULEBOOK §4.9.8) —
    #: the most blue-worth of ordnance a seat may hold. Unlike the dials
    #: above this is not doctrine and retuning it buys you nothing: the
    #: engine refuses the build regardless. It is here so the policy can
    #: decline gracefully instead of proposing an order it will lose.
    weapon_blue_cap: int = 600


#: The shipped economy. Fork-local, so retuning it cannot affect a rival.
DEFAULT_DIALS = OrbitDials()


# ── View readers ──────────────────────────────────────────────────
#
# Copied in rather than imported so the fork owns its whole orbit path
# and an attendee can follow it without leaving the directory. These are
# plumbing, not policy — they read the engine's view shape and nothing
# more. Changing them is almost never what you want.


def _my_harvesters(view: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """All harvesters the seat owns, in stable id order."""
    entities = (view.get("entities") or {}).get("mine") or []
    rows = [ent for ent in entities if ent.get("type") == "harvester"]
    rows.sort(key=lambda r: str(r.get("id") or ""))
    return rows


def _seat_of(view: Mapping[str, Any]) -> str:
    """The viewer's seat id, defaulting to ``p1`` on a stripped view."""
    meta = view.get("meta") or {}
    hud = view.get("hud") or {}
    return str(meta.get("player") or hud.get("player") or "p1")


def _seat_day_rng(
    view: Mapping[str, Any], seat: str, salt: str = "",
) -> _random.Random:
    """Seeded RNG for ``(session, seat, day, salt)``.

    Determinism matters here: the same view must replan to the same buy,
    or a transient retry silently changes what the seat owns.
    """
    meta = view.get("meta") or {}
    hud = view.get("hud") or {}
    session_id = str(meta.get("session_id") or "")
    day = int(hud.get("day") or meta.get("day") or 0)
    return _random.Random(f"{session_id}|{seat}|{day}|{salt}")


def _blue_purity_total(view: Mapping[str, Any]) -> int:
    """Rolled-up BLUE the seat is holding — the weapons currency."""
    orbit = view.get("orbit") or {}
    total = orbit.get("blue_purity_total")
    if total is not None:
        try:
            return int(total)
        except (TypeError, ValueError):
            pass
    blue = 0
    for p in orbit.get("hoard_parcels") or []:
        if str(p.get("colour", "")).upper() == "BLUE":
            try:
                blue += int(p.get("purity", 0) or 0)
            except (TypeError, ValueError):
                continue
    return blue


# ── Board-aware buying (Phase 2, IMPROVEMENT_STRATEGIES_PHASE2.md §1) ──
#
# The rest of this module is a pure function of credits/BLUE/fleet state,
# by design (see module docstring's "known holes"). These three readers are
# the one deliberate exception: cheap, best-effort board signals that make
# the weapon thresholds CONDITIONAL rather than flat, without touching how
# repairs/fleet/probes are decided. Each degrades to "no signal" (False /
# None) on a stripped test view rather than raising, so a caller that never
# populates these fields gets today's flat-threshold behavior exactly.


def _redsign_live(agent_view: Mapping[str, Any]) -> bool:
    """Any live redsign at all — mine or a rival's."""
    return bool(agent_view.get("redsign"))


def _finder_near(agent_view: Mapping[str, Any]) -> bool:
    """A rival's probe is sitting on/near a live redsign RIGHT NOW.

    Reuses ``supersede.py``'s own finder-detection (same function
    ``harness.py``/``seam_control.py`` already call) rather than a second
    intel path. Best-effort: any failure reads as "no finder", never a
    crash — this is advisory, not a legality gate.
    """
    if not agent_view.get("redsign"):
        return False
    try:
        return bool(_supersede._redsign_finder_cells(agent_view))
    except Exception:
        return False


def _rival_just_rearmed(agent_view: Mapping[str, Any]) -> bool:
    """Did any rival's PUBLIC weaponised-blue total just rise?

    Reads the reload-timing diff ``_v7/opponent_weapons.py`` already
    computes and stores every night (``WeaponEstimate.reload_note``) —
    READ-ONLY (``load_estimates``, never ``update_estimates``): this
    function must never mutate that store itself, or it would corrupt the
    prior/updated diff the night phase relies on by consuming the
    comparison a phase early. Best-effort: no estimates yet (day 1, or a
    stripped test view) reads as False, never a crash.
    """
    meta = agent_view.get("meta") or {}
    session_id = str(meta.get("session_id") or "")
    if not session_id:
        return False
    viewer = _seat_of(agent_view)
    try:
        estimates = _opponent_weapons.load_estimates(session_id, viewer)
    except Exception:
        return False
    return any(
        "just re-armed" in str(getattr(est, "reload_note", "") or "")
        for est in estimates.values()
    )


def _fleet_value_estimate(nights_left: Any) -> float:
    """What one more harvester is worth for the rest of the season.

    ``nights_left`` may be ``None`` (no ``hud.season_day_cap`` on a
    stripped view) — reads as 0.0, the same "no signal, no effect"
    contract every board-read helper in this module follows.
    """
    if nights_left is None:
        return 0.0
    return max(0, int(nights_left)) * _CALIBRATED_RED_PER_HARVESTER_PER_NIGHT


def _weapon_value_estimate(agent_view: Mapping[str, Any]) -> float:
    """What a weapon buy is worth THIS board, priced against the richest
    live redsign (if any) via the SAME calibrated model
    ``option_economics.denial_value_estimate`` already uses for
    CONTEST_DENY/CONTEST_DENY_EMP/CONTEST_DENY_CHAFF pricing. 0.0 (not a
    guess) when no live redsign exists — a weapon bought on a quiet board
    is genuinely speculative, and pricing it above 0 would be inventing a
    number the board doesn't support.
    """
    best = 0.0
    for row in (agent_view.get("redsign") or []):
        if not isinstance(row, Mapping):
            continue
        center = row.get("center")
        if not (isinstance(center, (list, tuple)) and len(center) == 2):
            continue
        try:
            beacon = (int(center[0]), int(center[1]))
        except (TypeError, ValueError):
            continue
        try:
            est = _option_economics.denial_value_estimate(beacon, agent_view)
        except Exception:
            est = None
        if est is not None:
            best = max(best, float(est.get("expected_pts", 0) or 0))
    return best


# ── The policy ────────────────────────────────────────────────────


def plan_orbit_actions(
    view: Dict[str, Any],
    *,
    weapons_enabled: bool = True,
    dials: OrbitDials = DEFAULT_DIALS,
    whitewalker_committed: bool = True,
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """Plan this seat's orbit submission (RULEBOOK §4).

    Returns ``(actions, rationale, whitewalker_bought)`` — wire-format
    orbit actions ready for ``engine.submit_orbit_actions``, the prose
    that lands on the card, and whether this call is the one that bought
    the scripted opening strike's EMP (see priority 0 below).

    Pure spending in priority order, with no slot cap: credits and BLUE
    are the only limit.

        0. Buy the "whitewalker EMP" opening strike's one EMP, the
           moment it is affordable — before anything else, and only
           once (see ``whitewalker_committed``).
        1. Repair every damaged harvester — a dead rig earns nothing.
        2. Build a harvester if under the fleet cap and affordable.
        3. Build weapons from surplus BLUE (skipped when disabled).
        4. Top the probe magazine up toward :attr:`OrbitDials.probe_target_stock`.

    Probes come last on purpose. Shipping RED is automatic and free, so
    there is no bid to hold credits back for; probes soak up whatever the
    fleet did not need.

    ``weapons_enabled=False`` is the no-weapons tutorial opponent
    (RED_HARVEST_LITE) — it skips priorities 0 and 3 and changes nothing
    else.

    ``whitewalker_committed`` — set ``False`` by the caller (``orbit.py``)
    until the scripted strike has been bought (or fired). Once bought,
    the caller persists a flag and passes ``True`` forever after, so
    priority 0 fires exactly once per game; every subsequent EMP/chaff
    buy goes through the ordinary priority-3 economy below.
    """
    orbit = view.get("orbit") or {}
    credits = int(orbit.get("credits", 0))
    cap_used = int(orbit.get("harvester_cap_used", 0))
    cap_max = int(orbit.get("harvester_cap_max", dials.harvester_cap))
    prices = orbit.get("ship_prices") or {}
    repair_cost = int(prices.get("repair", dials.repair_cost))
    probe_cost = int(prices.get("probe_build", dials.probe_build_cost))
    harvester_cost = int(
        prices.get("harvester_build", dials.harvester_build_cost),
    )

    blue_total = _blue_purity_total(view)
    weapon_stock = orbit.get("weapon_stock") or {}
    emp_stock = int(weapon_stock.get("emp", 0) or 0)
    chaff_stock = int(weapon_stock.get("chaff", 0) or 0)
    snap_stock = int(weapon_stock.get("snap", 0) or 0)
    weapon_prices = orbit.get("weapon_prices") or {}
    emp_price = weapon_prices.get("emp") or {}
    emp_blue_cost = int(emp_price.get("blue", dials.emp_blue_cost))
    emp_credit_cost = int(emp_price.get("credits", dials.emp_credit_cost))
    chaff_price = weapon_prices.get("chaff") or {}
    chaff_blue_cost = int(chaff_price.get("blue", dials.chaff_blue_cost))
    chaff_credit_cost = int(
        chaff_price.get("credits", dials.chaff_credit_cost),
    )
    snap_price = weapon_prices.get("snap") or {}
    snap_blue_cost = int(snap_price.get("blue", dials.snap_blue_cost))
    snap_credit_cost = int(snap_price.get("credits", dials.snap_credit_cost))
    # v1.34 — the arsenal ceiling (RULEBOOK §4.9.8), needed by priority 0
    # as well as priority 3, so it is computed once up top.
    weapon_blue_cap = int(
        ((view.get("meta") or {}).get("rules") or {}).get(
            "weapon_blue_cap", dials.weapon_blue_cap
        )
    )
    held_weapon_blue = 0
    for kind, price in (weapon_prices or {}).items():
        if not isinstance(price, Mapping):
            continue
        held_weapon_blue += (
            int(weapon_stock.get(kind, 0) or 0) * int(price.get("blue", 0) or 0)
        )

    def _room_for(blue_cost: int) -> bool:
        return held_weapon_blue + blue_cost <= weapon_blue_cap

    # Phase 2 (IMPROVEMENT_STRATEGIES_PHASE2.md §1) — board-aware buying.
    # Read once, used by both the SNAP tier and the always-build/roll
    # tiers below. All three degrade to False/large on a stripped test
    # view, which is what recovers today's flat-threshold behavior.
    redsign_live = _redsign_live(view)
    finder_near = _finder_near(view)
    rival_rearmed = _rival_just_rearmed(view)
    hud = view.get("hud") or {}
    meta = view.get("meta") or {}
    day = int(hud.get("day") or meta.get("day") or 0)
    day_cap = int(hud.get("season_day_cap") or 0)
    nights_left = (day_cap - day) if day_cap > 0 else None

    actions: List[Dict[str, Any]] = []
    descriptors: List[str] = []
    remaining = credits
    whitewalker_bought = False

    # LEGACY PATH, KEPT ON PURPOSE (v1.30). A new season never reaches
    # here — the simulator settles the terminal orbit itself rather than
    # asking, precisely BECAUSE "nothing worth buying" was the only
    # answer anyone ever had. It still fires for a season persisted
    # mid-final-orbit by a pre-v1.30 build. Delete it only once no such
    # save can exist.
    if bool(orbit.get("final_orbit")):
        return [], (
            "final settlement orbit: RED ships and GREEN clears "
            "automatically — nothing worth buying"
        ), False

    # Priority 0: the "whitewalker EMP" opening strike — buy the one EMP
    # it fires, ahead of repairs/fleet/probes, the moment blue and
    # credits allow it. Fires at most once per game; see harness.py /
    # whitewalker.py for the night-phase targeting that spends it.
    if weapons_enabled and not whitewalker_committed:
        if blue_total >= emp_blue_cost and remaining >= emp_credit_cost and _room_for(
            emp_blue_cost
        ):
            actions.append({"a": "build_emp", "count": 1})
            remaining -= emp_credit_cost
            held_weapon_blue += emp_blue_cost
            whitewalker_bought = True
            descriptors.append(
                f"WHITEWALKER: bought the opening-strike EMP first "
                f"(blue {blue_total}, {emp_credit_cost}c)"
            )
        else:
            descriptors.append(
                f"WHITEWALKER: EMP not yet affordable (blue {blue_total}/"
                f"{emp_blue_cost}, credits {remaining}/{emp_credit_cost})"
            )

    # Priority 1: repair every damaged harvester.
    for harv in [h for h in _my_harvesters(view) if bool(h.get("damaged"))]:
        if remaining < repair_cost:
            descriptors.append(
                f"deferred repair on {harv.get('id')} (need {repair_cost}c, "
                f"have {remaining}c)",
            )
            continue
        actions.append({"a": "repair", "unit": str(harv.get("id"))})
        remaining -= repair_cost
        descriptors.append(f"repaired {harv.get('id')} ({repair_cost}c)")

    # Priority 2: build a new harvester if under cap and affordable.
    if cap_used >= cap_max:
        descriptors.append(
            f"skipped harvester build (fleet at cap {cap_used}/{cap_max})",
        )
    elif remaining >= harvester_cost:
        actions.append({"a": "build_harvester"})
        remaining -= harvester_cost
        descriptors.append(f"built harvester ({harvester_cost}c)")
    else:
        descriptors.append(
            f"deferred harvester build (need {harvester_cost}c, "
            f"have {remaining}c)",
        )

    # Priority 3: weapons, tiered on rolled-up BLUE. Chaff leads the
    # always-build band because an empty rack loses the egress jam, and
    # the jam is the cheapest denial in the game.
    # v1.34 — the arsenal ceiling (RULEBOOK §4.9.8) and the held-blue
    # tally are computed up top now (priority 0 needs them too); this
    # section just consumes them via the closures below.
    def _afford_emp() -> bool:
        return (
            blue_total >= emp_blue_cost
            and remaining >= emp_credit_cost
            and _room_for(emp_blue_cost)
        )

    def _afford_chaff() -> bool:
        return (
            blue_total >= chaff_blue_cost
            and remaining >= chaff_credit_cost
            and _room_for(chaff_blue_cost)
        )

    def _afford_snap() -> bool:
        return (
            blue_total >= snap_blue_cost
            and remaining >= snap_credit_cost
            and _room_for(snap_blue_cost)
        )

    # Phase 2 — final-night weapon discipline. A charge bought with this
    # few nights left may never get a night to fire; priority 0
    # (whitewalker) is a separate, earlier block and is unaffected.
    final_night_no_weapons = (
        nights_left is not None and nights_left <= dials.final_night_weapon_cutoff
    )
    if final_night_no_weapons:
        descriptors.append(
            f"weapon buying skipped ({nights_left} night(s) left — a charge "
            "bought now may never get a night to fire; budget goes to "
            "fleet/probes instead)"
        )

    # Phase 2 — board-aware thresholds. Multipliers on the flat dials, not
    # new absolute numbers, so a stripped test view (no redsign/last_night
    # data) recovers today's behavior exactly at multiplier 1.0.
    effective_snap_buy = dials.blue_snap_buy * (
        dials.urgent_snap_multiplier if finder_near else 1.0
    )
    if (redsign_live and emp_stock == 0 and chaff_stock == 0) or rival_rearmed:
        _weapon_urgency = dials.urgent_weapon_multiplier
    else:
        _weapon_urgency = 1.0
    effective_always_build = dials.blue_always_build * _weapon_urgency
    effective_emp_roll = dials.blue_emp_roll * _weapon_urgency

    # Priority 3pre: SNAP, opportunistically, well below the chaff/EMP
    # surplus thresholds — it is cheap enough (100 blue) that waiting for
    # a 250-300 blue surplus just to afford chaff/EMP means never buying
    # the one weapon that can deny a same-hour landing (RULEBOOK §4.9.4).
    if (
        weapons_enabled
        and not final_night_no_weapons
        and blue_total > effective_snap_buy
        and snap_stock < dials.snap_stockpile_cap
        and _afford_snap()
    ):
        actions.append({"a": "build_snap", "count": 1})
        remaining -= snap_credit_cost
        held_weapon_blue += snap_blue_cost
        urgent_note = " — finder probe on a live redsign, buy it now" if finder_near else ""
        descriptors.append(
            f"built SNAP (blue {blue_total} > {effective_snap_buy:.0f}) — "
            f"cheapest denial in the game, buy it early{urgent_note}"
        )

    if weapons_enabled and not final_night_no_weapons and not _room_for(
        min(emp_blue_cost, chaff_blue_cost)
    ):
        # Say the cap out loud rather than letting it read as "cannot
        # afford" — an agent at the ceiling with a full wallet is a
        # different situation, and the descriptor is what a fork reads
        # when it wonders why its build never fired.
        descriptors.append(
            f"weapon build skipped (holding {held_weapon_blue} of the "
            f"{weapon_blue_cap} blue arsenal cap)"
        )
    elif (
        weapons_enabled
        and not final_night_no_weapons
        and blue_total > effective_always_build
        and cap_used < cap_max
        and _fleet_value_estimate(nights_left) > _weapon_value_estimate(view)
    ):
        # Phase 5 (IMPROVEMENT_STRATEGIES_PHASE2.md §5) — arms-race EV.
        # A tie-breaker, not an override: only intercepts the SAME
        # always-build tier the flat threshold already gated into, only
        # when there's still fleet room to grow into. A rich, contested
        # live redsign prices _weapon_value_estimate well above 0 and
        # this branch is skipped — the regression guard named in the
        # implementation plan.
        descriptors.append(
            f"weapon build deferred (blue {blue_total} > "
            f"{effective_always_build:.0f}, but {int(nights_left or 0)} "
            "night(s) of fleet growth outvalue it on tonight's board) — "
            "budget conserved toward a harvester instead"
        )
    elif (
        weapons_enabled
        and not final_night_no_weapons
        and blue_total > effective_always_build
    ):
        if chaff_stock < dials.chaff_stockpile_cap and _afford_chaff():
            actions.append({"a": "build_chaff", "count": 1})
            remaining -= chaff_credit_cost
            descriptors.append(
                f"built CHAFF for egress jam (blue {blue_total} > "
                f"{effective_always_build:.0f})"
            )
        elif emp_stock < dials.emp_stockpile_cap and _afford_emp():
            actions.append({"a": "build_emp", "count": 1})
            remaining -= emp_credit_cost
            descriptors.append(
                f"built EMP (blue {blue_total} > {effective_always_build:.0f})"
            )
        elif _afford_chaff():
            actions.append({"a": "build_chaff", "count": 1})
            remaining -= chaff_credit_cost
            descriptors.append("built CHAFF (blue surplus top-up)")
        elif _afford_emp():
            actions.append({"a": "build_emp", "count": 1})
            remaining -= emp_credit_cost
            descriptors.append("built EMP (blue surplus top-up)")
        else:
            descriptors.append(
                f"weapon build wanted (blue {blue_total}) but unaffordable "
                f"(have {remaining}c)"
            )
    elif (
        weapons_enabled
        and not final_night_no_weapons
        and blue_total > effective_emp_roll
        and emp_stock < dials.emp_stockpile_cap
        and _afford_emp()
    ):
        emp_rng = _seat_day_rng(view, _seat_of(view), salt="emp-build")
        if emp_rng.random() < dials.emp_roll_chance:
            actions.append({"a": "build_emp", "count": 1})
            remaining -= emp_credit_cost
            descriptors.append(
                f"built EMP (blue {blue_total} > {effective_emp_roll:.0f}, "
                f"50% roll hit)"
            )
        else:
            descriptors.append(
                "skipped EMP build (blue surplus but 50% roll missed)"
            )

    # Priority 4: top the probe magazine up. A flat "build 2" ran dry and
    # left harvesters unable to hot-drop, so top up toward the target in
    # one batched build, bounded by credits and current stock.
    current_probe_stock = int(orbit.get("probe_stock", 0) or 0)
    want = max(0, dials.probe_target_stock - current_probe_stock)
    affordable = remaining // probe_cost if probe_cost > 0 else 0
    build_n = min(want, affordable)
    if build_n >= 1:
        actions.append({"a": "build_probe", "count": int(build_n)})
        remaining -= build_n * probe_cost
        descriptors.append(
            f"built {build_n} probe(s) ({build_n * probe_cost}c) — "
            f"stock {current_probe_stock}→{current_probe_stock + build_n}"
        )
    elif want <= 0:
        descriptors.append(
            f"probe magazine full (stock {current_probe_stock}≥"
            f"{dials.probe_target_stock})"
        )
    else:
        descriptors.append(
            f"deferred probe build (need {probe_cost}c, have {remaining}c)",
        )

    rationale = (
        f"orbit day plan ({credits}c available): "
        + "; ".join(descriptors)
        + f". Carryover {remaining}c."
    )
    return actions, rationale, whitewalker_bought
