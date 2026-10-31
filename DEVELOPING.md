# Developing Wholesale EV Schedule

Technical/contributor documentation. For what the integration does and how
to set it up, see [README.md](README.md). If you're an AI coding agent,
start at [AGENTS.md](AGENTS.md) instead — it covers repo-specific workflow
conventions this file doesn't repeat.

## Structure

```
custom_components/wholesale_ev_schedule/
  scheduler.py      pure scheduling algorithm, no HA dependency
  coordinator.py    reads price/charger entities, runs scheduler.py, persists state
  providers.py      registry of known price-source shapes (Octopus Energy, AgilePredict,
                    Agile Rates API) for the rates, forecast and short-range forecast slots
  price_parsing.py  pure helpers for reading price points out of attributes (dotted key
                    paths, estimate flags) -- no HA dependency, like scheduler.py
  config_flow.py    multi-step setup + options: name, poll interval, providers
  entity.py         shared base — forces the <prefix>_ entity_id namespace
  sensor.py         primary + diagnostic (hidden-by-default) output sensors
  binary_sensor.py  charging_desired output
  number.py         live inputs: hours required, boost duration, scheduling tolerances,
                    opportunistic target hours/max price (opt-in, see below)
  datetime.py       ready_by, opportunistic_ready_by — live inputs
  select.py         charge_override, optimization_algorithm — manual overrides
  switch.py         opportunistic_charging_enabled — live pause/resume (opt-in)
  button.py         boost_cancel, stop, reset — actions
  brand/            icon/logo shown in the HA UI (local brand images, HA 2026.3+)
```

## Architecture notes

- **`scheduler.py` is pure** — no Home Assistant imports, no I/O. Everything
  in it is unit-tested directly (`tests/test_scheduler.py`) without a `hass`
  fixture. If you're changing the scheduling algorithm itself, this is the
  file to touch, and it should stay portable.
- **`coordinator.py` is the only place that touches HA state.** It reads the
  configured price/charger entities, calls into `scheduler.py`, and persists
  live inputs (`ready_by`, `required_hours`, tolerances, `charge_override`,
  the in-progress schedule) via a `Store` so they survive restarts.
- **`desired` (the `binary_sensor`) is computed purely from the schedule plus
  `charge_override`** — there's deliberately no charger-specific "work state"
  entity wiring. Wire `charging_desired` to whatever actually controls your
  charger (a switch, an API call, your own automation) and use the override
  select to force it on/off regardless of the computed schedule. This keeps
  the integration portable across charger brands.
- **`find_optimal_slots` supports three interchangeable selection algorithms**
  (`OPTIMIZATION_ALGORITHM_*` in `const.py`, chosen at runtime via the
  `optimization_algorithm` select entity, `coordinator.optimization_algorithm`):
  `_find_optimal_slots_greedy` (the original implementation, unchanged,
  default), `_find_optimal_slots_optimal` (an exact DP-based search — see
  `_min_cost_for_run`'s docstring for the state-machine details, and its
  docstring's note on why `max_price` is checked only where a block's size is
  actually decided, not incrementally while it grows), and
  `_find_optimal_slots_hybrid` (the same DP restricted to a handful of
  representative block sizes per run, for speed). All three share
  `_min_max_slots_per_block` and `_solve_with_per_run_costs`; added for #55
  (greedy can lose to a combination — often one larger window — whose
  combined total is cheaper than picking the single cheapest window first).
- **The "last block may be short" relaxation is exact in `optimal`/`hybrid`, validated in
  `greedy`.** The rules (`tests/oracle.py`): exactly `required` slots, all ending by
  `ready_by`, all inside a contiguous run long enough to hold a minimum block, at most ONE
  maximal group shorter than `min_block_hours` (wherever it falls), and every group's average
  *raw* price within `max_price`. The DP algorithms build that allowance into the state
  (`_min_cost_for_run` carries a "short block used" flag; `_solve_with_per_run_costs` is a
  knapsack over runs with that flag), so they are exact -- proven against exhaustive
  enumeration on tens of thousands of random markets (`tests/test_scheduler_oracle.py`). An
  earlier version chose the cheapest full blocks first and then bolted on a top-up, which is
  not exact: the best answer can use a dearer full block precisely so a lone cheap slot
  elsewhere can be the short one (it lost to the true optimum on ~1% of small markets).
  `greedy` can't do that, so it still fills a remainder with `_best_topup` (slots grown from
  existing blocks plus at most one contiguous window, validated on the blocks they produce);
  the original version picked the cheapest *individual* leftover slots, which scattered
  1-slot blocks and ignored `max_price` (found on a live install, pinned by
  `tests/test_block_and_price_constraints.py`). Related: with a `max_price` and no
  `max_block_hours`, `_min_cost_for_run` tracks real block length (bounded by the requested
  slot count) instead of collapsing all "free" lengths into one state, because the cap is a
  whole-block average. `hybrid` currently returns the same answers as `optimal` (its
  per-run size restriction turned out to bind only on `max_block_hours`, where it was a bug:
  a block cap bounds each *block*, not how many blocks a run can hold).
- **All scheduling arithmetic is on UTC instants.** Python compares and subtracts two aware
  datetimes that share a tzinfo by *wall clock* and keys the repeated autumn hour as one entry,
  so slots built from local times lost or mis-measured time across a clock change (the 25-hour
  day dropped two slots, runs split at the change, durations were off by an hour). `scheduler.to_utc`
  is the single conversion; the coordinator ingests prices as UTC and `_sessions_from_slots`
  renders session start/end back into local time, so what is shown and persisted still carries the
  local offset. `tests/test_scheduler_dst.py` builds slots from real `ZoneInfo` datetimes.
- **Rollover credits before it resets.** `_accrue_delivered_hours` runs *before* the `ready_by`
  rollover zeroes the counters and consumes what it counts (idempotent). A session ending exactly
  at `ready_by` is first seen as ended on the rollover refresh; crediting it afterwards counted it
  toward the new cycle and the next requirement read as already delivered (no charging on
  alternate days). Pinned in `tests/test_sim_scenarios.py`.
- **An in-progress block follows what the user changes.** Sessions are planned once and carried
  forward while they run, so `clip_active_session` (running block) and `fit_future_sessions`
  (upcoming, used when prices are unavailable and the stored plan stands in) cut them to the tier's
  remaining allowance and deadline. Only *whole slots that ran* are credited on an early stop: a
  part-slot would be rounded up by the delivered-hours accounting and leave the car short. Setting
  the required hours is still a deliberate *fresh request* (delivered hours do not carry over).
- **One short block per request, remembered across re-plans.** Every re-plan solves only the
  remainder, so without memory each short block that ran earned a fresh allowance and a day could
  contain several (the min block exists to stop charger cycling). `_short_blocks_used` counts
  finished required blocks shorter than the minimum (persisted, reset with `_reset_required_progress`)
  and `find_optimal_slots(avoid_short_block=True)` then prefers a schedule needing none, falling
  back only if there is no other. `tests/test_sim_cost_quality.py` compares what was paid with the
  brute-force optimum and checks at most one short block actually runs per request.
- **Risk adjustment is sign-aware.** `compute_effective_price` divides a doubtful *positive* price
  by its credibility but multiplies a *negative* one, otherwise a forecast -5p looks better than a
  real -5p ("plunge pricing").
- **Non-half-hourly feeds are normalised.** `price_parsing.normalize_to_half_hours` turns a regular
  hourly feed into two slots per point and averages regular 15/10/5-minute feeds into half hours;
  half-hourly and irregular feeds are untouched (needs 4+ points and >=80% regular gaps, and it
  never invents prices across a hole).
- **Stored state is sanitised on load.** `_parse_stored_float` rejects non-finite/non-numeric/boolean
  values and clamps to the *same* ranges the number entities enforce (`const.RANGE_*`, one
  definition); dates are bounded, sessions must be plausible, enums are checked with `isinstance`
  first (a stored list is unhashable), and session lengths always come from start/end
  (`session_slots`), never the display field. A NaN/inf previously left the entry stuck in
  `setup_retry`. `tests/test_stored_state.py` is the matrix.
- **Removing an instance deletes its stored state** (`async_remove_entry`; the key is built by
  `coordinator.stored_state` so create and delete cannot drift).
- **`next_slot` is the earliest upcoming session** across both tiers (`prune_and_classify` returns
  futures in time order, sorted on instants so it holds across a clock change).
- **There's no live `max_block_hours` entity** — it was removed as an
  unnecessary second knob (it only ever capped window *selection* size
  without guaranteeing an actual rest period; genuine multi-block scheduling
  already happens naturally whenever cheap price dips are separated by a
  pricier period — see `tests/test_tolerances_and_reload.py::test_multi_block_scheduling_splits_across_separate_cheap_dips`).
  `find_optimal_slots` in `scheduler.py` still *accepts* a `max_block_hours`
  parameter and is tested with it (`tests/test_scheduler.py`) since it's a
  reusable, well-understood pure capability — it's just not wired to
  anything in the coordinator/entity layer right now. Re-add a `number`
  entity for it if a real need comes up.
- **`ready_by` has no fixed default and never just "expires".** On first
  setup, and again every time the current `ready_by` is reached, it's set to
  the next occurrence of `self._default_ready_by_hour`, at least
  `self._default_ready_by_day_offset` days out (`scheduler.next_ready_by`) —
  see `_async_update_data` in `coordinator.py`. This makes "charge N hours by
  7am" a standing daily target instead of something that errors out or needs
  resetting by hand.
- **Opportunistic charging (issue #53) is a second, opt-in tier layered on
  top of the required one, gated behind `CONF_ENABLE_OPPORTUNISTIC_CHARGING`
  (`const.py`, default `False`).** `switch.py`/`number.py`/`datetime.py`/
  `sensor.py`'s opportunistic entity classes are only added when that option
  is set — an install that never opts in gets zero new entities/behaviour
  (`tests/test_opportunistic_config_flow.py`'s
  `test_opportunistic_disabled_produces_exactly_the_default_entity_set`
  guards this structurally: the default `FULL_OPTIONS`-based setup must
  still equal the pre-existing `EXPECTED_ENTITY_IDS` exactly).
  `_compute_sessions` (`coordinator.py`) runs the required tier first,
  completely unchanged, then `_compute_opportunistic_sessions` searches only
  the candidate slots required didn't claim, capped by
  `opportunistic_ready_by`/`opportunistic_max_price` instead of
  `ready_by`/`max_price`. Each resulting session is tagged
  `"tier": SESSION_TIER_REQUIRED` or `SESSION_TIER_OPPORTUNISTIC` (missing
  tag defaults to required, so a session persisted before this feature
  existed still accrues correctly on upgrade) — `_accrue_delivered_hours`
  routes a completed session's duration to `_delivered_hours` or
  `_delivered_opportunistic_hours` by that tag, mirroring the required
  tier's own delivered-hours accounting (issue #57 / PR #58) exactly.
  `opportunistic_ready_by` has no independently-tracked value of its own the
  way `ready_by` does: it's always re-derived as
  `ready_by + default_opportunistic_offset_days` at the moment `ready_by`
  itself rolls forward (the same rollover block in `_async_update_data`,
  and also in `async_load_stored_state`/`async_reset`, both of which set
  `ready_by` directly and so must derive it themselves rather than rely on
  that rollover check, which only fires when `ready_by <= now_dt` — never
  true immediately after either of those sets a fresh future `ready_by`). A
  live edit via `async_set_opportunistic_ready_by` is a one-cycle-only
  override that snaps back to the derived value on the next such rollover.
  Turning the setup-time toggle back off explicitly removes any
  already-created opportunistic entities from the registry
  (`_async_remove_opportunistic_entities_if_disabled` in `__init__.py`,
  matched by the `"_opportunistic_"` infix in each entity's `unique_id`)
  rather than leaving them orphaned/unavailable.
- **`async_setup_entry` (`__init__.py`) registers a wall-clock-aligned minute
  tick via `async_track_time_change(hass, ..., second=0)`, in addition to the
  coordinator's own `update_interval_minutes` polling.** A `DataUpdateCoordinator`'s
  built-in timer fires at a fixed delta from whenever the coordinator was
  constructed (integration setup/reload time), not from wall-clock minute
  boundaries — so without this, `charging_desired` transitions land at an
  arbitrary second offset (e.g. 10:03:04) and can lag a real slot boundary by
  up to `update_interval_minutes`. The extra tick calls
  `coordinator.async_request_refresh()` once a minute at :00 seconds so
  schedule-driven state always catches up to a slot boundary within ~60s,
  regardless of `update_interval_minutes`.
- **Solves are memoised per tier (`_solve` in `coordinator.py`, issue #60).** The
  minute tick above re-runs the whole schedule every minute, but the answer only
  changes when prices, settings or the 30-minute slot boundary do -- and with
  `optimal`/`hybrid` one solve can take hundreds of ms on a Pi, on the event loop.
  `_solve` skips `find_optimal_slots` when its inputs equal the previous call's for
  that tier (required / opportunistic cached independently). The key is every
  candidate's `(date_time, raw_price, effective_price, tier)` plus the requirement,
  deadline, `min_block_hours`, cap and algorithm; `effective_price` is what makes
  credibility tiers shifting as time passes invalidate it. Only the selected
  `date_time`s are cached and the result is rebuilt from the *current* candidate
  dicts. If you make `find_optimal_slots` read anything new, **add it to the key**
  (`tests/test_solve_cache.py` mutation-checks this -- dropping any input from the
  key fails a test). In-memory only; never persisted.
- **Setup-time "sensible defaults" (issue #2).** `CONF_DEFAULT_REQUIRED_HOURS`
  / `CONF_DEFAULT_GAMBLE_TOLERANCE` / `CONF_DEFAULT_MAX_PRICE` /
  `CONF_DEFAULT_MIN_BLOCK_HOURS` / `CONF_DEFAULT_READY_BY_HOUR` /
  `CONF_DEFAULT_READY_BY_DAY_OFFSET` (`const.py`) are config-flow options,
  shown in `base_schema()` (`config_flow.py`) on both the initial `user` step
  and the options `init` step. They're read once at coordinator construction
  into `self._default_*` instance attributes, each falling back to the
  original hardcoded `DEFAULT_*` constant (or `0` for the day offset) when
  unset — so an install that never touches them behaves exactly as before.
  They're used only in two places: `async_load_stored_state()`'s fresh-install
  fallback and `async_reset()`. Changing them via the options flow triggers
  the same full reload that `CONF_UPDATE_INTERVAL_MINUTES` already does
  (`_async_update_listener` in `__init__.py`), so no separate reload wiring
  was needed. The day-offset select has exactly four options — value `0`
  ("Next day": as soon as possible, i.e. today if the hour hasn't passed yet,
  otherwise tomorrow) through `3` ("Next day + 3") — matching the wording
  requested in the issue.

## Entity naming

`entity.py` sets each entity's `entity_id` explicitly (rather than letting HA
derive it from the friendly name) to `<platform>.<prefix>_<suffix>`, where
`prefix` is the slugified instance name chosen during setup (defaults to
`wholesale_ev_schedule` — see `const.DEFAULT_NAME`). The config flow enforces
uniqueness on that slug via the entry's `unique_id`, so two instances can
never end up sharing a prefix. `tests/test_entity_naming.py` and
`tests/test_multi_instance.py` cover both the prefix guarantee and
no-collision-with-the-pyscript-original guarantee (this was ported from
[homeassistant-pyscripts](https://github.com/TheCJGCJG/homeassistant-pyscripts),
and is designed to run alongside it, or any other EV-charging setup, on the
same HA instance without colliding).

## Adding a new price provider

1. Add an entry to `RATE_PROVIDERS` or `FORECAST_PROVIDERS` in `providers.py`
   with the attribute/key names and unit multiplier that source's entities
   expose.
2. Add one config_flow step mirroring the existing `rates_octopus_energy` /
   `forecast_agile_predict` ones, and wire it into `_ProviderStepsMixin`.
3. Add strings/translations for the new step.

"Custom" already covers any source not yet modelled this way — it asks for
the attribute/key names and multiplier directly.

## Testing

All tests run inside Docker (`python:trixie` from Docker Hub), in parallel (`-n auto`, via pytest-xdist; use `-n 0` to debug one test):

```bash
./run-tests.sh              # run tests
./run-tests.sh --coverage   # with an HTML coverage report
./run-tests.sh --shell      # interactive shell in the container
```

What each test file covers:
- `tests/test_scheduler.py` — unit tests for the pure scheduling algorithm, including max_block_hours splitting (the pure capability), next_ready_by (including a DST spring-forward transition), the price-summary diagnostics, and the "optimal"/"hybrid" algorithms against every greedy scenario plus issue #55's reported case
- `tests/test_block_and_price_constraints.py` — min-block and max_price as guarantees on the returned schedule, across all three algorithms, plus seeded randomised invariants.  Guards the top-up step: remainders must be one contiguous, under-`max_price` window, never scattered single slots
- `tests/test_calculating_state.py` — the state sensor shows `calculating` mid-recompute for every user-edit path (not periodic refreshes), never flips `charging_desired`, and always clears, even if the refresh fails
- `tests/test_solve_cache.py` — the solve memo: unchanged refreshes/minute ticks don't re-solve, every input that can change the answer (settings, prices, slot boundary, per-slot fields) forces one, opportunistic-only edits re-solve only that tier, and a seeded random sequence of edits/clock moves always equals an uncached recompute
- `tests/test_price_parsing_helpers.py` — pure unit tests for `price_parsing.py`: dotted-path resolution (literal-key-wins, malformed/non-mapping/empty-segment inputs, read-only mappings, falsy values) and the conservative estimate-flag interpretation
- `tests/test_short_range_scheduler.py` — pure scheduler coverage for the short-range tier: the full source-precedence matrix in both input orders, ties, unknown sources, per-slot coverage gaps, the credibility override and its clamping, `estimated`/`confidence` on sessions, and end-to-end ranking with every algorithm
- `tests/test_short_range_forecast.py` — coordinator-level behaviour against a real entry: entries that never opted in are untouched (incl. stale keys), parsing of the real Agile Rates API shape and every malformed-row case, DST 23/25-hour days (the 25-hour autumn merge is a strict `xfail` for a pre-existing, provider-independent dedupe bug), precedence over the real feeds, what gets scheduled (including the 10:00 -> 16:00 estimate->final transition), fallback when the entity vanishes, the opportunistic tier, diagnostics, and solve-cache interplay
- `tests/test_short_range_config_flow.py` — the new dropdown/steps/validation, routing with the other providers, the options-flow guarantees for existing installs (re-saving untouched changes nothing else; opting in/out; switching provider clears the old flag key; the #38-style stale-`None` entity), legacy option shapes of every era still setting up, and a structural check that every step/field has strings in both `strings.json` and `translations/en.json`
- `tests/test_optimization_algorithm.py` — the optimization_algorithm select entity: default value, persistence via the select entity, survival across restarts, an invalid stored value degrading to greedy, reset restoring the default
- `tests/test_integration_smoke.py` — full config/options flow, entity registration, idle vs error state on a fresh setup given the new defaults
- `tests/test_providers.py` — custom rates/forecast provider branches, "no forecast" branch, blank custom attribute/key fields rejected, re-enabling forecast after "none"
- `tests/test_multi_instance.py` — two instances with distinct names don't collide; duplicate-name/slug is rejected; blank name is rejected
- `tests/test_entity_naming.py` — entity_id prefix guarantee, no collision with the pyscript original
- `tests/test_price_parsing.py` — custom rate/forecast attribute & key names, unit multiplier, missing forecast entity, gamble tolerance
- `tests/test_charge_override.py` — auto/force-on/force-off, including overriding an active slot or boost, persistence across restarts, an invalid stored value degrading to auto
- `tests/test_tolerances_and_reload.py` — max_price/min_block_hours enforcement, genuine multi-block scheduling without a max_block_hours knob, options-triggered reload picking up a changed update interval, unschedulable reason naming the actual cause
- `tests/test_boost_stop.py` — boost start/self-reset, boost cancel, stop vs reset (reset restores every default), boost_ends_at visibility
- `tests/test_diagnostics.py` — diagnostic sensors are hidden by default, price-summary diagnostics populate even when idle, block/upcoming-block sensors, live tuning number entities
- `tests/test_edge_cases.py` — malformed/missing/non-finite price data, malformed stored state (ready_by, boost_end, sessions, numeric tuning fields), ready_by rolling forward instead of erroring once passed, active-session persistence across a price refresh or a total price-data gap, natural boost expiry, NaN rejected at the live number-entity setter
- `tests/test_estimated_cost.py` — next-slot average price and the estimated-cost sensor derived from it via assumed_charge_kwh, including live updates when the number changes, persistence, reset restoring its default, and the calculation breakdown exposed as attributes
- `tests/test_minute_tick.py` — the wall-clock-aligned minute tick (`async_track_time_change` in `__init__.py`) triggers a coordinator refresh at the next :00 second boundary well before a full `update_interval_minutes` has elapsed, and its listener is torn down on unload
- `tests/test_long_running_simulation.py` — continuous operation across several simulated days including a real DST transition (no crashes, ready_by keeps rolling over correctly), and a genuine unload+re-setup restart producing a brand-new coordinator instance that rehydrates fully from storage
- `tests/test_required_hours_completion.py` — issue #57/PR #58 regression: hours_remaining doesn't bounce back up once a completed session's required_hours has genuinely been met, and delivered_hours resets correctly when required_hours is changed again
- `tests/test_week_long_scheduling.py` — required-tier delivered-hours correctness across a multi-day ready_by horizon: varying horizon lengths, a multi-session schedule surviving a price reshuffle after the first session completes, and max_price enforced per-block (not globally) both when it does and doesn't block the requirement
- `tests/test_opportunistic_config_flow.py` — opportunistic entity-creation gating: disabled produces exactly the pre-existing default entity set, enabled produces that set plus the four opportunistic entities, all carrying the instance prefix
- `tests/test_opportunistic_scheduling.py` — required-vs-opportunistic layering (opportunistic never claims a slot required still needs), opportunistic_max_price independent of max_price, a zero target being a no-op, the live enabled switch pausing/resuming without clearing configuration, and opportunistic delivered-hours accrual/reset
- `tests/test_opportunistic_ready_by_rollover.py` — opportunistic_ready_by defaulting to ready_by + offset, a manual edit holding only until the next required-ready_by rollover, rolling forward correctly across several rollovers, and a manual edit to required ready_by NOT itself perturbing opportunistic_ready_by
- `tests/test_opportunistic_lifecycle.py` — Stop zeroes the opportunistic target but keeps tuning preferences/deadline; Reset restores opportunistic defaults and re-derives opportunistic_ready_by immediately (not left unset until the next natural rollover); opportunistic state survives a genuine restart; a pre-opportunistic stored session with no "tier" key defaults to the required counter on upgrade
- `tests/test_opportunistic_reconfigure.py` — enabling/disabling opportunistic charging via the actual options-flow UI walk adds/removes exactly the opportunistic entities, and a direct options update takes effect after reload
- `tests/sim.py` + `tests/test_sim_*.py` — **the simulator**. `Sim` runs the real integration through simulated days: a seeded `PriceWorld` (Agile rates published ~15:00Z for the next day, a forecast whose error grows with horizon, day-ahead estimates that firm up), any feed layout (`Shape`), HA timezone, outages, restarts (`sim.restart()` builds a brand-new coordinator from storage), user actions (`sim.act(...)`) and a notional charger, and calls `check_invariants` after every step (sorted non-overlapping sessions, state/desired/override consistency, never over-committed, deadline and price cap respected, next/active slot match the list, a fresh plan respects the minimum block, delivered hours monotonic, stored state serialisable). Scenario files: `test_sim_scenarios.py` (daily cycles in 11 timezones across clock changes; deadline hours in the DST gap/fold; the rollover regression), `test_sim_restart.py` (a restart changes nothing, at every step), `test_sim_user_actions.py` (hours/deadline/cap/override/boost/stop/reset mid-run, in-progress blocks), `test_sim_feeds.py` (source strategies, publication timing, outages incl. edits during an outage), `test_sim_opportunistic.py`, `test_sim_reconfigure.py` (options changes on a running install), `test_sim_cost_quality.py` (paid vs the brute-force optimum; at most one short block runs), and `test_sim_chaos.py` (seeded random everything; a failure prints the seed and last actions -- replay it, then write a focused test).
- `tests/oracle.py` + `tests/test_scheduler_oracle.py` — an exhaustive brute-force reference for slot selection (enumerates every subset, keeps valid ones, takes the cheapest). `optimal` must equal it exactly, nothing may ever return an invalid schedule, greedy/hybrid may never beat it, and `avoid_short_block` is checked against its `max_short=0` variant. Verified at 30,000 random markets during development; CI runs ~1,000.
- `tests/test_scheduler_dst.py`, `tests/test_next_ready_by_sweep.py` — pure scheduler functions with real `ZoneInfo` datetimes across London/New York/Sydney/Lord Howe clock changes; `next_ready_by` swept over eight zones x every half hour x eight hours x three offsets
- `tests/test_clip_active_session.py`, `tests/test_session_ordering.py`, `tests/test_scheduler_pricing_edges.py`, `tests/test_price_grid.py` — pure unit tests for block clipping/fitting, chronological sessions, negative/zero/huge prices, and the 30-minute-grid normaliser
- `tests/test_stored_state.py` — every stored field x garbage / non-finite / out-of-range / wrong type; extreme dates; implausible sessions; lossless round trip
- `tests/test_entity_contracts.py`, `tests/test_instances.py`, `tests/test_config_matrix.py`, `tests/test_price_formats.py`, `tests/test_registry_and_hygiene.py` — entities through real service calls and the attribute contract in every state; multi-instance isolation and removal cleanup; all 72 provider combinations created via the real config flow and fed their own data layout (asserting each configured source actually contributed); timestamp/number/ordering/granularity robustness; provider-registry and release-hygiene guards

## Linting and security scanning

Also run inside Docker, config lives in `pyproject.toml`:

```bash
make lint           # ruff check — unused imports, import order, bugbear/pyupgrade/simplify rules
make format          # ruff format — apply the formatter
make format-check    # ruff format --check — fail if anything's unformatted
make security        # bandit (SAST on custom_components/) + pip-audit (dependency CVEs)
make check           # test + lint + format-check + security, all of the above
```

`ruff check`, `ruff format --check`, and `bandit` are all enforced in CI (see
below) and must pass before merging. `pip-audit` runs in CI too, but only
informationally (`continue-on-error`) — transitive-dependency CVEs are what
`.github/dependabot.yml` is for; hand-pinning a transitive package to chase a
clean `pip-audit` run isn't worth fighting pip's resolver over.

## CI / releases

`.github/workflows/ci.yml` runs everything Docker-based in a single `checks`
job — `ruff check`, `ruff format --check`, `bandit` (all blocking), then the
test suite, then `pip-audit` (informational only, `continue-on-error`).
They're one job rather than separate `test`/`lint`/`security` jobs because
they all run the same image: splitting them just meant paying for a separate
runner boot + Buildx setup + cache round-trip per check on top of an
identical build. Order is cheapest-first, so a lint failure exits before
paying for a full pytest run. The image itself is built once via
`docker/build-push-action` with the GitHub Actions cache backend
(`cache-from`/`cache-to: type=gha`) rather than a plain `docker build` —
without it, every run would pip-install the entire
`pytest-homeassistant-custom-component` dependency tree from scratch, since
GitHub-hosted runners start with no Docker layer cache. With it, only a
`requirements-*.txt` change actually triggers a real rebuild. `runs-on:
ubuntu-latest` is GitHub's standard hosted runner. The test suite is large
(thousands of simulated-day tests) and runs in parallel with pytest-xdist
(`-n auto --maxprocesses=4` in `pytest.ini`), so it scales with the runner's
cores: keep the repository's runner size at the default (Settings → Actions →
Runners) rather than shrinking it.

`release` depends on `checks` and publishes a GitHub release (zipping
`custom_components/wholesale_ev_schedule/`) whenever `manifest.json`'s
`version` changes and doesn't already have a matching tag — so bumping the
version is what triggers a release, not every commit.

Two more workflows run alongside it:
- `.github/workflows/validate.yml` — `home-assistant/actions/hassfest` (manifest
  schema, HA integration conventions) and `hacs/action` (HACS distribution
  requirements), on push/PR to `main` plus a weekly schedule.
- `.github/dependabot.yml` — weekly automated PRs for both `pip` (the
  `requirements-*.txt` files) and `github-actions` dependencies.

CodeQL is deliberately *not* a workflow file here — this repo already has
GitHub's default CodeQL setup enabled (Settings → Code security → Code
scanning), which covers Python and GitHub Actions and runs without a
workflow file or any of our own worker-minutes. Adding a custom
`codeql.yml` alongside it produces a hard "configuration error" (the two
setups conflict) rather than running twice.

## Brand images

`custom_components/wholesale_ev_schedule/brand/` holds `icon.png`/`logo.png`
(+ `@2x` variants), rasterized from `logo.svg`. As of HA 2026.3, custom
integrations ship brand images locally in this folder rather than via a PR to
the separate `home-assistant/brands` repo — HA picks them up automatically,
no manifest changes needed. To regenerate after editing the SVG:

```bash
docker run --rm -v "$(pwd)/custom_components/wholesale_ev_schedule:/work" \
  python:trixie \
  bash -c "apt-get update -qq && apt-get install -y -qq librsvg2-bin && \
    rsvg-convert -w 256 -h 256 /work/logo.svg -o /work/brand/icon.png && \
    rsvg-convert -w 512 -h 512 /work/logo.svg -o /work/brand/icon@2x.png && \
    rsvg-convert -w 256 -h 256 /work/logo.svg -o /work/brand/logo.png && \
    rsvg-convert -w 512 -h 512 /work/logo.svg -o /work/brand/logo@2x.png"
```
