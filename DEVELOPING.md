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
  providers.py      registry of known price-source shapes (Octopus Energy, AgilePredict)
  config_flow.py    multi-step setup + options: name, poll interval, providers
  entity.py         shared base — forces the <prefix>_ entity_id namespace
  sensor.py         primary + diagnostic (hidden-by-default) output sensors
  binary_sensor.py  charging_desired output
  number.py         live inputs: hours required, boost duration, scheduling tolerances
  datetime.py       ready_by — live input
  select.py         charge_override, optimization_algorithm — manual overrides
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

All tests run inside Docker (`public.ecr.aws/docker/library/python:trixie`):

```bash
./run-tests.sh              # run tests
./run-tests.sh --coverage   # with an HTML coverage report
./run-tests.sh --shell      # interactive shell in the container
```

What each test file covers:
- `tests/test_scheduler.py` — unit tests for the pure scheduling algorithm, including max_block_hours splitting (the pure capability), next_ready_by (including a DST spring-forward transition), the price-summary diagnostics, and the "optimal"/"hybrid" algorithms against every greedy scenario plus issue #55's reported case
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
ubuntu-latest` is already GitHub's smallest/cheapest hosted runner — there's
no smaller tier to drop to.

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
  public.ecr.aws/docker/library/python:trixie \
  bash -c "apt-get update -qq && apt-get install -y -qq librsvg2-bin && \
    rsvg-convert -w 256 -h 256 /work/logo.svg -o /work/brand/icon.png && \
    rsvg-convert -w 512 -h 512 /work/logo.svg -o /work/brand/icon@2x.png && \
    rsvg-convert -w 256 -h 256 /work/logo.svg -o /work/brand/logo.png && \
    rsvg-convert -w 512 -h 512 /work/logo.svg -o /work/brand/logo@2x.png"
```
