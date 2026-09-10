# stark_direwolf — improvement strategies

Research doc, not an implementation plan. Each strategy below is a candidate
for a future implementation prompt (next step, not this doc). Ordered by
leverage, with the cross-cutting finding first because it explains why the
other six categories are all gated the same way.

Baseline: `tabula_v12` (this fork's parent) plus the `whitewalker` EMP
one-time opening strike already shipped here. Everything below is *new*
ground — it does not re-litigate whitewalker.

---

## 0. The cross-cutting finding: most gaps are pricing gaps, not capability gaps

`docs/TEACHING_WEAPONS.md` and `harnesses/emp_harvest_test/README.md` both
converge on the same root cause for "V12 buys weapons and never fires them":
a bare EMP salvo renders **`+0`** on the option menu, next to harvest chains
carrying real four-figure numbers. A model told to pick the highest-scoring
option never picks zero, no matter how much doctrine prose argues for it.

Research into the rest of the harness (`option_economics.py`) turned up the
**same defect, independently, in three other places**:

| Surface | Where it renders as 0 | Evidence |
|---|---|---|
| Weapons (EMP/chaff/SNAP) | menu option score | `docs/TEACHING_WEAPONS.md` §6, `emp_harvest_test/README.md` |
| BLUE | `option_economics.py:14-17` — *"BLUE scores 0 (it is the fissile spend surface, not standings)"* | `yield_breakdown` reports it as a separate `blue_fissile` field, never converted to points |
| Denial / next-night value (supersede, `CONTEST_DENY`) | scored in the same per-turn pool as a red grab that banks tonight | `doctrine.py:505-510` — demoted explicitly *because* "it banks nothing tonight" |
| Probe information value | not scored at all — probes are priced by tonight's `area_gain` only | `_v7/probe_hints.py` |

`option_economics.py`'s `blind_estimate` function is the in-tree proof that
the fix works: a "blind" harvest comb used to price as the literal word
"unknown" against options carrying real numbers, and lost by default. Giving
it a defensible number (odds × halo purity, calibrated over 28 offline
seasons) let it start competing on merit.

**The strategic thesis for this whole document:** wherever a play's real
value doesn't show up as a number on the menu, the model won't choose it,
regardless of how good the doctrine prose is. The single highest-leverage
category of fix is *pricing*, not *new mechanics* — most of the mechanics
already exist.

---

## 1. Weapons beyond EMP

Whitewalker solved one problem (V12 buys EMP and never fires it) with one
tool (deterministic, one-time, bypass the model). It does not touch chaff,
SNAP, or repeated/adaptive EMP use. Those need different tools because they
aren't a single scripted play — they're recurring situational decisions.

### 1.1 Chaff has literally no fire path — build it

Confirmed by exhaustive grep: `chaff_flare` does not exist anywhere in
`chat_schema.py`, `agency.py`, or `packager.py`. The agent can buy chaff
(`orbit_policy.py` priority 3) and can *read* that a rival fired chaff
(`last_night.py`), but has no verb to fire its own. `chaff_react` — the
thinker's only chaff-related field — is explicitly **reported, never
enforced** (`packager.py` comments, fix 2.10/OBS-27).

This is a prerequisite gap: nothing else about chaff can be built until the
verb rung exists (schema enum + sanitizer passthrough + a `spend_chaff`
packager emitter, mirroring `_Packer.spend_emp` from this session's
whitewalker work).

**Two concrete plays once the verb exists**, both rule-blessed by
`OUTSTANDING_ISSUES.md` issue #16 (RULEBOOK v1.14):

- **Counter-EMP chaff** — chaff now cancels a same-hour EMP launch, *and the
  cancelled launch does not spend the rival's munition* (their charge stays
  in their rack, only their slot burns). Flare on the hour you predict a
  rival salvo; you're immune on your own launch hour. `doctrine.py` already
  frames chaff as "the sole mechanic that cancels an H1 landing" — it's
  identified, just never actionable.
- **Offensive H1 denial** — chaff is the *only* weapon that can deny a
  rival's guaranteed-certain pure grab on hour 1 (EMP can't: it resolves
  below the vision snapshot and a same-hour cloud doesn't block that hour's
  landing — see 1.3 below). A predictable rival H1 smash-and-grab is a chaff
  target.

Both need the same pricing fix as EMP: bundle with a harvest wave so the
compound shows real EV (see §1.4's pattern), not a bare `+0` chaff-only
option.

### 1.2 SNAP is never bought or used at all

The cheapest weapon on the ladder (100 blue) has zero offensive footprint.
`orbit_policy.py` never emits `build_snap`. The only SNAP code is
*defensive*: rival-arsenal inference (`opponent_weapons.py`), a doctrine
block deliberately kept short because "there is no read to make" for the
*victim* side, and `PRSNAP*` cover options (a second probe so one deleted
cell doesn't unsight a landing).

SNAP is mechanically distinct from EMP in exactly the way that matters for
offense: **SNAP resolves above the hour-start vision snapshot** — it kills a
probe (or damages a harvester) *before* that hour's legality is judged, so a
SNAP on a rival's finder probe or a rival's about-to-land hot-drop probe
genuinely denies that hour, where an EMP salvo on the same target would not
(see 1.3).

**Concrete play:** buy 1-2 SNAP early (100 blue, cheap relative to EMP/chaff)
and use it to delete a rival's redsign *finder* probe (the probe sitting
within Chebyshev-4 of a live beacon, lighting the pure for them) —
`supersede.py` already computes exactly this target set for the supersede
menu; a SNAP option can reuse the same targeting and be strictly better than
a supersede for this purpose because it denies *tonight*, not just future
nights.

### 1.3 Know precisely which weapon voids which landing (targeting correctness)

This is a rules-precision item worth encoding directly rather than leaving
to prompt prose, because getting it backwards wastes a charge for nothing:

| Weapon | Resolves relative to hour-start snapshot | Same-hour effect on a landing |
|---|---|---|
| SNAP | **above** (before) | Landing **refused** — probe dead before its vision counted, drop refused, outing not spent |
| EMP | **below** (after) | Landing **succeeds and harvests once** if the cloud is freshly spawned that hour; only blocks harvest from cloud-established-at-hour-start onward |
| Chaff | pre-empts both | Cancels the hour's action entirely; cancelled EMP/SNAP keeps its charge |

`OUTSTANDING_ISSUES.md` issue #24 records that this exact confusion used to
be a real engine bug *and* had been "written up as a feature" in the
rulebook's own prior commentary — i.e., this is a genuinely easy thing to get
backwards, including for the humans who wrote the rules. Any weapon-firing
logic (whitewalker's own targeting included, if extended) should be built
against this table explicitly, not against an assumption that "EMP blocks
whatever it touches this instant."

### 1.4 Generalize the compound-play pricing pattern from `emp_harvest_test`

`emp_harvest_test` solved "the model won't fire a +0 salvo" by never
offering a bare salvo — every weapon option is bundled with the harvest wave
it enables, priced as one option with one real number:

- **`SMASH_THEN_LOCK`**: drop-and-lift the pure now (certain points), then a
  *shaped* salvo (`scorch.shaped_salvo`) that darkens the mass ring around it
  while leaving the pure cell itself clear, so a second harvester can comb
  the ring once the cloud lifts. Three waves, one option ID, one score.
- **`RACE_CRASH_EMP`**: drop into contested ground expecting a collision
  (denial, not harvest), salvo with a hole over the contested cell, then
  re-drop under your own cloud's shelter once it's safe.

Both are skipped when their preconditions don't hold (no EMP stock, no known
pure, chaff in play cancelling the sequencing, <2 harvesters) — the
compound-play pattern is additive, not a replacement for the plain options.

**Task shape for stark_direwolf:** add the same wave-field vocabulary
(`emp_launch_at` / `emp_hole` / `emp_only` / `defer_until_clear`, or a chaff
equivalent) to `seam_control.py`'s pattern dataclass, and two or three named
compounds (an EMP one modeled on `SMASH_THEN_LOCK`, a chaff one — e.g.
"flare the hour you predict their grab, take it yourself next hour" — modeled
on the counter-EMP logic in 1.1). This is the mechanism that would let
*repeated* EMP/chaff use (beyond whitewalker's one scripted strike) actually
get chosen by the model, because each one carries a real yield number.

### 1.5 Turn the rival-arsenal decode from an adjective into a plan

`opponent_weapons.py`'s inference is essentially exact — since the engine
broadcasts weaponised blue exactly per seat every hour, `decode_rack` returns
every loadout consistent with that total. But every consumer downstream
collapses this rich structure into a single boolean (`_enemy_armed()` in
`option_economics.py`) used only to bump a generic collision-risk label.

Three concrete uses of data that's already being computed and thrown away:

- **Rack-conditional posture, computed not advised.** Doctrine already
  states the right rule in prose ("if only p3 has chaff and p3 isn't
  contesting your seam tonight, don't shorten every chain") — nothing
  enforces it. Feed the per-seat decode into the SHORT-GRAB/EXTEND posture
  decision directly.
- **Reload timing.** The decode is read fresh every turn, so diffing it
  night-over-night tells you whether a seat just fired (empty rack, can't
  punish tonight) or just bought (rack now heavier). Nothing currently
  computes this diff.
- **Aim-point avoidance for weapons, not just collisions.** `hint_dispersion.py`
  already implements deterministic per-seat offset rotation — but only for
  friendly-collision avoidance. The same mechanism, retargeted at "don't be
  the obvious cell when a decoded rack includes an EMP," is a small reuse.
  A genuine bait (a cheap unit on the obvious cell, the real grab offset)
  doesn't exist at all yet.

---

## 2. Probing strategy

### 2.1 The v1.29 "warmer ground" gradient is computed by the engine and read by nobody

This is probably the single cleanest, most self-contained improvement in
this whole document. `RULEBOOK.md` §2.2 (v1.29) rebuilt terrain *specifically*
so that probing is a deduction, not a lottery: every jackpot sits inside a
graded halo — `mass` chunks within a 1.5-cell radius, a `vein` shoulder out to
4.2 cells, both well inside the 12-cell separation between distinct jackpots
so two halos can never merge into one ambiguous read. The changelog quotes
the before/after: jackpots with no nearby `mass` went from 79% to 1.4%.

Nothing in the current harness reads this. `probe_hints.py`'s `edge_promise`
term is a **flat scalar sum** of purity around a candidate disk — it can tell
you "there's red near here," but not *which direction* the deposit centre is,
which is exactly the read the terrain rebuild was designed to support.

**Task:** a gradient-following probe placer — given observed vein/mass
cells, estimate the implied deposit centre using the known halo radii as the
model, and bias probe placement *up-gradient* rather than toward the biggest
unexplored hole. This changes "probe the biggest fog gap" into "probe where
the terrain says the jackpot is."

### 2.2 No multi-night probe planning — every placement is single-night

Every probe-scoring function evaluates only *tonight's* value
(`area_gain`/`edge_promise`, or frontier's coverage-vs-enemy-proximity
sampling). What exists is entirely negative: don't re-probe a cell you
already covered, don't probe where a rival's landings suggest they're
working, skip a supersede target about to expire. Nothing *positive* is
scheduled — there's no notion of "these two disks expire on night 5, place
their replacements on night 4 so I never go dark on the seam I'm actively
working," and no deliberate map-tiling plan across the 3-night probe
lifetime.

**Task:** a probe-portfolio planner that reasons about expiry (probes live
`SOC_PROBE_LIFETIME_NIGHTS` = 3 nights) against ongoing campaigns, rather than
treating every night's probe menu as if it were the only night that exists.

### 2.3 Unspent probes are a silent loss with no mechanical backstop

Harvesters have a *mechanical* completion pass — any harvester left idle
after the model's plan gets the best unused chain auto-deployed
(`packager.py`'s completion pass, backstopped again in `move_sanitizer.py`).
**Probes have no equivalent.** The only enforcement is a doctrine line
("selecting only one PR while probes sit in stock is the single most common
way this seat throws away a night's vision") — prose, not code. Compounding
this, `orbit_policy.py` tops probe stock up to a flat target *last* in the
buy order ("probes soak up whatever the fleet did not need") and admits
"nothing here reads the board" — probe count is a budget residual, never a
function of what the board actually needs scouted.

**Task:** add a probe completion pass mirroring the harvester one — any
probe stock left unspent after the model's plan gets deployed onto the
best unused offered probe/frontier/supersede hint, the same asymmetry-closing
move already proven for harvesters.

---

## 3. Harvester utilization

Good news first: **occupancy is already solved, twice** — a packager
completion pass deploys any idle harvester onto the best unused offered
chain, backstopped again in the move sanitizer. The remaining gaps are about
*quality*, not whether harvesters sit idle.

### 3.1 The completion pass is only as good as the menu it draws from

If nothing above the "junk" floor is offered that night, the completion pass
deploys onto near-junk just to avoid a bigger loss (an idle harvester banks
zero). Occupancy is the metric being optimized; yield-per-outing isn't.
Nothing currently distinguishes "deployed onto something decent" from
"deployed onto something merely non-zero."

### 3.2 Chain length / exposure tradeoff is entirely delegated to the model

Doctrine states this explicitly: the packager compiles a chosen chain
*verbatim* (minus hard hazard cuts) and deliberately does not shorten a walk
for value/exposure reasons — "that judgement is YOURS." Doctrine also names
flinching (cutting a chain too short out of excess caution) as "the single
most expensive habit this seat has." Both directions of the mistake are
named as known failure modes, and both are currently unenforced — entirely
prompt-dependent.

**Task:** consider a lightweight EV-based route trim/extend suggestion
surfaced on the menu option itself (not a silent compiler cut — doctrine is
right that silent cuts have caused real losses before, per the OBS-27
history) — e.g., annotate each chain option with "shortening by N steps
avoids M cells of collision-risk overlap at a cost of K expected points," so
the tradeoff is visible rather than purely instinctual.

### 3.3 Route planning is repair, not optimization

Drop relocation (`_relocate_drop`) is deliberately restricted to cells the
chosen option already names — correct as a hallucination-safety rule ("
anything further afield is a different option and belongs on the menu
instead"), but it means there's no actual path optimization anywhere, only
post-hoc legality repair of a path the model already committed to.

### 3.4 Known open bug: the heuristic baseline itself misreads a seam choice

`docs/OUTSTANDING_ISSUES.md` issue **#27** is the one open (not-done) bug in
the entire log, and it's squarely a chain/seam-selection quality issue: the
deterministic `RED_HARVEST` heuristic fails `two_seams_choose_one` — it
walks the wrong seam of the two on offer. Three terrain/pricing changes
(v1.29 grading, blind-seam pricing, last-night settlement) are named
suspects, and the issue notes explicitly *not* to "fix" it by loosening the
test assertion until it's confirmed the expected seam is actually still the
better one under current pricing. Worth resolving before or alongside any
seam-ranking work in this fork, since it may indicate the underlying seam
scorer (shared by heuristic and LLM paths) has drifted.

---

## 4. BLUE economy — five gates, and the one under all of them

`tabula_v12/README.md` documents this as five gates, verified in code:

1. A purity floor (`_BLUE_GRAB_MIN = 192`) before blue is offered at all —
   coupled to the sanitizer's own blue-loot floor, so they must move together.
2. Blue must not cost a harvester a stronger red chain (`_STRONG_CHAIN_RED_MIN
   = 150`), gated by a raw count comparison rather than an EV comparison.
3. Blue is only "requested" when the vault is short *and* ≥2 harvesters are
   live — a conjunction that's easy to loosen incorrectly.
4. Blue chain hints are suppressed entirely unless requested (gate 3 feeds
   gate 4).
5. Doctrine explicitly ranks blue below red for a scarce harvester, in both
   the prose block and the menu option blurbs the model reads first.

The README's own framing: "loosening one lever alone usually does nothing,
because another still gates it." Confirmed structurally true — and the
reason is the same §0 finding: **BLUE scores exactly 0 in the menu's own
yield model** (`option_economics.py` reports it as a separate `blue_fissile`
field, never converted to points). Loosen all five gates and every blue
option still renders `red 0 / blue N` next to red options carrying
three-figure totals.

**The real task is gate zero, not gates 1-5:** give BLUE a points-equivalent
price — blue → weapon → denial/tempo → points — using the same compound-play
pricing pattern as §1.4. This is likely why the README calls gap 2 (blue)
"partly why gap 1 (weapons) stays unexploited" — the causality runs both
ways: undervalued blue means underfunded weapons, and unfired weapons mean
blue has no realized value to point at.

Two things worth respecting when building this: `weaponised_blue` saturates
at a hard cap (600), so a naive linear blue→points conversion should account
for diminishing returns near the cap; and the "Final Refinery" terminal-orbit
mechanic already exists as an aggressive leftover-blue sink at season end —
the night phase currently plans nothing toward feeding it.

---

## 5. Memory and journal usage

The harness carries a genuinely rich set of durable memories — journal
(intent/reflection), a hazard-cell union, enemy-trail tracking, enemy-probe
landing history, redsign-survival decay. Persistence itself was a real,
now-fixed bug (`OUTSTANDING_ISSUES.md` #22: V12 played entire Snowflake
seasons with no cross-night memory at all until v1.19, because the backing
table was never deployed and every write silently no-op'd inside a bare
`except`).

### 5.1 Every enforced memory is a negative constraint

Everything that's mechanically *enforced* (not just carried and hoped the
model reads it) is a "don't" — refuse this drop, discount this stale echo,
penalize proximity to this enemy landing, decay this seam's assumed
survival. **There is no positive learned model anywhere.** Nothing
accumulates "the seam at (17,7) was rich, return to it," "p2 always opens
northwest," "p3 tends to fire EMP around hour 3." The journal (prose,
LLM-authored) is the only place anything positive could live, and whether
the model actually changes strategy because of it is entirely prompt-
dependent — the fix that shipped in v1.19 restored *persistence*, not
*action*.

### 5.2 No rival behavior model, despite the data already being collected

Enemy probe-landing history is already accumulated season-long — and used
*only* as a proximity penalty for the seat's own next probe placement.
Nowhere is there a per-seat profile: preferred quadrant, aggression level,
typical weapon-firing timing, whether they contest redsigns or avoid them.
The raw signal (public probe launches, public settlement manifests, public
weaponised-blue totals) is already flowing through the harness for other
purposes — building a lightweight per-opponent profile from data already in
hand is a comparatively cheap addition.

### 5.3 Nothing scores a play by what it teaches you

There's no exploration/information-value term anywhere in the scoring model
— probes are priced purely by tonight's direct area/edge value (see §2),
never by what they'd reveal for future planning.

---

## 6. Doctrine / menu economics — the time dimension is missing entirely

`option_economics.py` is a genuinely sophisticated per-turn scorer — real
engine-truth yield math, blind-comb pricing calibrated over 28 offline
seasons, overlap-claim detection (so two options claiming the same pure don't
both get counted), self-crush verdicts, collision-risk annotation. All of it
is **per-turn**. `day`/`day_cap` reach the scorer in exactly three places,
and all three collapse to a single boolean or a one-line adjustment — there
is no term anywhere for "this play's value accrues on a *later* night."

Concretely missing:

- **No seam commitment across nights.** Nothing represents "I hold this
  seam; the ring is worth returning to on night 4." Only doctrine prose
  ("bank the sure value now, set up the kill for tomorrow").
- **No sacrifice-tonight-for-bigger-later-night.** The one option whose
  entire value is next-night (`CONTEST_DENY`) is explicitly *demoted* in
  doctrine specifically because the scorer can't price its upside — "it
  banks nothing tonight, so it is the cautious pick, not the default."
- **No discounting / terminal value.** Vision and denial are worth
  objectively more early in a season than late; the scorer has no time axis
  at all.
- **The highest-leverage decision on a two-redsign night is explicitly
  unscored.** Doctrine hands the model the contest-vs-secure fork with "no
  rule can pick it for you — think it through" and "let the score tilt the
  fork, do not hard-code it" — a deliberate design choice, but it leaves the
  single biggest strategic fork in the game running on vibes.

**This is arguably the single highest-leverage item in this whole document**,
because it's the same "+0 today" defect as weapons and blue, applied to the
time axis instead — and fixing it would simultaneously unblock supersedes,
probes-as-investment, and blue-as-ordnance, all three of which are currently
underpriced for exactly this reason. Task shape: add an explicitly-labelled
second value column — "banks tonight" vs. "sets up a later night, worth
~X" — to the option menu, rather than forcing every play through a single
tonight-only number.

---

## Suggested priority order

1. **§6 (time-axis pricing)** — highest leverage, unblocks three other
   categories at once, and is a scoring/menu change rather than new game
   mechanics.
2. **§1.4 (compound-play pattern, generalized)** — proven pattern already
   exists in `emp_harvest_test`; porting it covers chaff, repeated EMP, and
   gives §4 (blue) somewhere to point its new pricing.
3. **§2.1 (gradient-following probes)** — self-contained, doesn't depend on
   anything else, directly exploits an engine mechanic that's currently
   totally unread.
4. **§1.1/§1.2 (chaff verb + SNAP purchase/use)** — mechanical prerequisites,
   moderate effort, needed before §1.4's chaff compound can exist.
5. **§2.3/§3.1 (probe completion pass, chain-quality annotation)** — smaller,
   well-scoped, mirrors an existing proven pattern (harvester completion).
6. **§5 (positive memory / rival modeling)** — valuable but the least
   mechanically constrained; worth doing once the pricing fixes above give
   the model something to act on beyond "don't."
7. **§3.4 (open heuristic bug, issue #27)** — not urgent for LLM-path work,
   but worth resolving before trusting seam-ranking changes, since the
   heuristic and LLM path may share the underlying scorer.
