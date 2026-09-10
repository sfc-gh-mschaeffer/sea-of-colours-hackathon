# stark_direwolf — improvement strategies, phase 2 (beating a strong, weapon-literate opponent)

Research doc, not an implementation plan — same role as `IMPROVEMENT_STRATEGIES.md`,
which this one follows and does not repeat. Read that doc first; everything
in Phases 1-8 there is already implemented (denial pricing, chaff/SNAP/EMP
verbs + compounds, gradient probes, per-seat armed checks, reload-timing
diff, rival behavior profile, chain-trim annotations, probe expiry). This
doc is about the NEXT tier of opponent: not V12 (which barely uses weapons at
all), but a genuinely strong, weapon-literate agent that will find and
exploit whatever is still static, reactive, or advisory-only in this seat.

Baseline for this doc: `stark_direwolf` as of the Phase-2-of-8 validation
pass (2026-09-10) — SNAP-fire and the chaff compound both confirmed working
end to end, CONTEST_DENY_EMP confirmed correctly gated (rare, not broken).
Validated by grepping 20 real seasons' local card records (`reports/seasons/`)
against the intended mechanics, not just unit tests.

---

## 0. The cross-cutting finding: this seat still only reacts to the board — it never anticipates it

Phase 1 of the prior doc fixed "value that doesn't show up as a number" —
that thesis held and paid off (CONTEST_DENY, EMP/chaff compounds, chain
tradeoffs all price correctly now). The next-tier defect is different in
kind: almost every decision this seat makes is a function of **what is true
right now** — current blue, current weapon stock, current rival vision, this
turn's redsign. Nothing budgets for **what will probably be true a few hours
or a few nights from now**, and nothing budgets for **what the rival can
infer about US** the same way `rival_profile.py` lets us infer about them.

A mediocre opponent (V12) never punishes this, because it isn't looking for
patterns either. A strong opponent will. Two concrete examples already
confirmed in this codebase:

- `orbit_policy.py`'s own docstring: *"Nothing here reads the board. Buying
  is a function of credits, BLUE and fleet state only."* Explicitly named as
  a "known hole, left deliberately (they are the exercise)."
- Doctrine tells the model to "space multi-harvester pickups >= 3 hours
  apart" and "avoid hour 6-9 pickups" (`prompt` text in `harness.py`'s
  assembled doctrine block) — but nothing in `packager.py` enforces or even
  checks this. It is advice a strong opponent can safely bet the model
  sometimes ignores under token pressure, the same way "the compiler will
  NOT trim your tail for you" was true and unenforced before Phase 7's chain
  annotations gave it a number.

**The strategic thesis for this whole document:** the highest-leverage
category of fix is no longer pricing (that's largely done) — it's closing
the gap between *reactive* and *anticipatory*, in both directions: predicting
what the rival will do, and controlling what the rival can predict about us.

---

## 1. Board-aware orbit buying (the file already flagged as the exercise)

`orbit_policy.py` buys weapons off flat BLUE thresholds
(`blue_snap_buy=150`, `blue_always_build=300`, `blue_emp_roll=250`) with
zero awareness of whether tonight's board justifies spending now versus
saving for fleet. Against a weapon-literate opponent this cuts both ways:

- **Under-armed when it matters.** Sitting at 140 blue with a live rival
  redsign and a finder probe on the board — exactly the situation
  `_rival_deny_chaff_pattern`/`_rival_deny_emp_pattern` want a charge for —
  and `blue_snap_buy=150` says wait one more turn regardless.
- **Over-armed when it doesn't.** Topping up chaff/EMP stockpile on a quiet
  night with no live contest anywhere, spending credits a sharper opponent
  is putting into a 3rd harvester or a repair instead.

**Task:** thread a cheap board-state read into `plan_orbit_actions` —
whether a live redsign exists (`agent_view.get("redsign")`), whether a rival
finder probe is already visible (reuse `supersede._redsign_finder_cells`,
already imported project-wide), whether a rival's weapon stock just rose
(reuse `_v7/opponent_weapons.py`'s existing `reload_note`/prior-blue diff).
Make the SNAP/chaff/EMP thresholds *conditional* dials rather than flat
ones: sharply lower when a live contest is on the board tonight, sharply
raise (or skip entirely) on a quiet night. This is additive to the existing
priority-3 tiering, not a rewrite — same dials-vs-policy split the module
docstring already prescribes (`OrbitDials` stays numbers; the new board read
lives in `plan_orbit_actions`).

**Test:** two synthetic views differing only in redsign/finder presence,
same blue total, asserting different SNAP/chaff decisions. Then the existing
`tests/test_orbit_policy.py` differential test against the shared planner
still needs to pass with `weapons_enabled=False` / a redsign-free view — the
new read must be strictly additive, never changing the V12-identical
baseline path.

## 2. Final-night weapon discipline

Confirmed by grep: `orbit_policy.py` only handles the engine's own terminal
`final_orbit` settlement flag. There is no logic for "this orbit is late
enough in the season that a weapon bought now may never get a night to
fire." A weapon bought on the last or second-to-last orbit is credits and
blue that could have gone to one final harvester or probe top-up instead —
pure waste if the season ends before it's used.

**Task:** read `hud.day` / `meta.season_day_cap` (already present on every
view; see `_seat_day_rng`'s own read of `hud.day`) and taper the weapon
priority in the last 1-2 nights of the season, unless a scripted or
already-queued use is guaranteed to fire that same night (whitewalker's
priority-0 EMP is exempt by construction — it fires immediately, never
banked). Redirect that credit/blue budget toward the probe/fleet priorities
instead on those final nights.

**Test:** a view at `day == season_day_cap` and one at `day ==
season_day_cap - 1`; assert weapon-build actions are suppressed (or reduced)
relative to an identical mid-season view, and that probe/harvester spend
absorbs the difference rather than the credits going unspent.

## 3. Counter-intelligence, aimed inward: are WE predictable?

`rival_profile.py` (Phase 6 of the prior doc) builds a model of THEIR habits
— preferred quadrant, redsign-contest rate. Nothing builds the mirror image
of US. A strong opponent will build exactly this against stark_direwolf, and
if our own pickup-hour rhythm, drop-offset pattern, or probe-placement cycle
clusters predictably, they'll time a SNAP/EMP/chaff to it. Doctrine text
already names the risk ("space pickups >=3 hours apart," "avoid hour 6-9")
but it's prose the model may or may not act on — the same unenforced-advice
shape Phase 7's chain-trim annotations existed to fix for chain length.

**Task, in two parts:**

- **3a. Self-profile.** A `self_profile.py` (or an addition to
  `rival_profile.py`, since the accumulation/read shape is identical) that
  tracks THIS seat's own recent pickup-hour distribution and drop-offset
  pattern across nights, using the exact same read-modify-write flag pattern
  `rival_profile.record_turn`/`get_profile` already established. Feed a
  warning line into the option menu (or the doctrine block) when this
  night's leading candidate would repeat a pattern already flagged as
  predictable — mirrors how `_armed_watcher` turned "someone might be armed"
  into a specific, checkable fact.
- **3b. Enforced (not advisory) jitter.** Reuse the seeded-RNG tie-shuffle
  pattern already proven in `_v7/probe_hints.py` (`_tie_shuffle`,
  `_PROBE_TIE_EPS`) to nudge pickup hour or drop offset within the safe
  window when two candidate plans are close in value — deterministic per
  `(session, seat, day)` so replays stay stable, but no longer a fixed
  default the model always reaches for under time pressure.

**Test:** feed `record_turn`-equivalent a synthetic multi-night sequence
where every pickup lands at hour 7; assert the self-profile flags it and
that a subsequent otherwise-tied plan gets jittered off hour 7.

## 4. Weapon-timing prediction, not just weapon-holding detection

Phase 5 (prior doc) tells us WHO is armed and whether they just fired or
rearmed (`WeaponEstimate.reload_note`). It does not tell us WHEN they tend to
fire. A sophisticated opponent has a rhythm — e.g., salvos on hour 3-4 after
a redsign goes public, every time. `rival_profile.py` already has the
accumulation infrastructure (per-seat, session/player-keyed, read-modify-
write) to extend from "quadrant preference" to "typical strike hour relative
to a redsign's discovery" — the same shape, one more counter.

**Task:** extend `rival_profile.py`'s per-seat payload with a
`strike_hour_offsets: List[int]` (or a running histogram) populated whenever
`last_night.py` reports a rival weapon launch this seat can attribute to a
specific redsign's discovery hour. Surface a `typical_strike_hour` in
`get_profile()`'s return, and feed it into `coverage_note()`
(`option_economics.py`) as a sharper version of the existing VERY HIGH
escalation — "beware, they weaponize ~hour 3-4 after discovery" instead of
the current generic "a rival holds EMP/chaff."

**Test:** synthetic multi-night sequence where a rival always launches on
hour 3 after a redsign; assert `typical_strike_hour` converges and that a
`coverage_note()` call on a fresh redsign for that seat's beacon reflects it.

## 5. DROP BLOCK as a real, selectable option

Confirmed by grep: `DROP BLOCK` (deliberately colliding on a contested cell
to deny a rival's loaded harvester — RULEBOOK-legal, referenced in
`option_economics.py:1441-1459` and `doctrine.py:265-286`) exists ONLY as
rationale prose. There is no menu option the model can select; it would have
to compose the coordinate itself, which is exactly the failure mode this
whole ID-based menu architecture (`agency.py`) was built to eliminate
everywhere else. A rival who plays DROP BLOCK against us confidently, while
we can only reach it (if at all) via free-form coordinate composition, is a
structural asymmetry.

**Task:** a `_drop_block_option` in `agency.py`, mirroring `_snap_option`'s
shape (built this session for exactly this kind of gap) — one option per
live-vision rival-occupied contested cell, `kind="drop_block"` (remember to
add it to `_KIND_HEADERS` — the Phase-2-of-8 validation pass found the SNAP
option was silently dropped from the rendered menu for exactly this reason;
do not repeat it), payload carrying the target cell, dispatched through the
ordinary `emit_chain`/drop path in `packager.py` since it is a normal drop,
just onto a cell `option_economics.py` currently only warns about.

**Test:** unit test confirming the option appears only when a rival-occupied
contested cell is in live vision, renders with the existing DROP BLOCK
rationale text, and compiles to a legal `drop` move through `pack_recipe`.

## 6. A real cost-benefit for the weapon arms race, not fixed dials

If both sides buy weapons aggressively, blue spent on ordnance is blue not
spent on fleet — a genuine resource race that static thresholds don't reason
about. Against V12 (which barely competes for blue) the dials don't matter
much; against an equally aggressive weapon-buyer they start to. This is the
same "+0 EV" pricing defect Phase 1 (prior doc) fixed at the menu level, now
one level up at the orbit-economy level: "is one more EMP worth more than
what a 3rd harvester banks for the rest of the season" is never actually
asked, just gated on a blue threshold.

**Task:** a lightweight EV comparison in `plan_orbit_actions` — reuse
`option_economics.denial_value_estimate`'s existing calibration (already
proven, already reused twice this session) to estimate what a weapon
purchase is worth THIS game state, and compare it against a rough per-
harvester nights-remaining × average-red-per-night estimate (derivable from
`orbit.blue_purity_total` history or a simple constant calibrated the same
way `_ASSUMED_RED_DENSITY`/`_ASSUMED_HALO_PURITY` were — see
`option_economics.py`'s own halo-trace calibration methodology for the
pattern to follow, not a number invented from vibes). Bias toward fleet when
the comparison favors it; this directly composes with Phase 1's board-aware
buying above rather than replacing it.

**Test:** given a view with a low-value board (no redsign, thin seams) and
enough blue for either a weapon or most of a harvester, assert the policy
now favors the harvester; given a view with a rich, contested redsign,
assert it still favors the weapon (regression-guards against overcorrecting
into never buying weapons).

---

## Suggested sequencing

1. and 2. (board-aware buying, final-night discipline) are the same file,
   same function, and 2 is nearly free once 1's board-read plumbing exists —
   do them together.
3. and 4. (self-profile, strike-hour prediction) both extend
   `rival_profile.py`'s already-proven accumulation shape — natural pair,
   moderate effort.
5. (DROP BLOCK) is small, mechanical, and a near-exact mirror of work
   already done twice this session (SNAP-fire option, chaff compound) —
   good warm-up or filler between the larger items.
6. (arms-race EV) depends on 1's board-read existing first, and is the most
   speculative/least-calibrated of the six — do it last, and validate its
   calibration the same way `denial_value_estimate` was validated (grep real
   season cards for the numbers it produces before trusting it in a head-to-
   head).
