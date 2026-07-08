# Wholesale EV Schedule

> **⚠️ Still evolving.** This has been run against a live Home Assistant
> instance and produces real schedules, but new features land quickly and
> each round is only verified against the Docker test suite before you try it
> live — read the CHANGELOG-style notes below before upgrading an existing
> install, since options/entities occasionally get restructured.

A Home Assistant custom integration that schedules EV charging across the
cheapest wholesale price windows. Ported from the scheduling algorithm in
[homeassistant-pyscripts](https://github.com/TheCJGCJG/homeassistant-pyscripts)
into a proper integration with a config flow for its inputs and `sensor`/
`binary_sensor`/`number`/`datetime`/`button` entities for its outputs, instead
of pyscript + `input_*` helpers.

Supports **multiple instances** side by side (e.g. one per car) and is
designed to run **alongside** an existing pyscript-based (or any other)
EV-charging setup on the same HA instance: every entity_id an instance
creates is forced to start with a prefix derived from that instance's name
(see "Entity naming" below), so instances — and integrations — can never
collide on entity_ids.

## Structure

```
custom_components/wholesale_ev_schedule/
  scheduler.py      pure scheduling algorithm, no HA dependency
  coordinator.py    reads price/charger entities, runs scheduler.py, persists state
  providers.py      registry of known price-source shapes (Octopus Energy, AgilePredict)
  config_flow.py    multi-step setup + options: name, charger, providers
  entity.py         shared base — forces the <prefix>_ entity_id namespace
  sensor.py         primary + diagnostic (hidden-by-default) output sensors
  binary_sensor.py  charging_desired output
  number.py         live inputs: hours required, boost duration, scheduling tolerances
  datetime.py       ready_by — live input
  button.py         boost_cancel, stop, reset — actions
  brand/            icon/logo shown in the HA UI (local brand images, HA 2026.3+)
```

## Configuration

The config flow (and its matching **Configure** options flow) walks through:

1. **Name + charger + poll interval + providers** — a name for this instance
   (becomes the entity_id prefix; give each instance a distinct one if
   running more than one), the charger work-state sensor and its "connected"
   states, how often to re-evaluate, and which named provider supplies your
   actual and forecast wholesale prices.
2. **Rates step** (shape depends on the provider chosen above):
   - *Octopus Energy* — just the current-day and next-day rates event entities.
   - *Custom* — the same two entities, plus the attribute/key names and a unit
     multiplier, for a source not modelled in `providers.py`.
3. **Forecast step** (skipped entirely if you pick "None"):
   - *AgilePredict* ([agilepredict.com](https://agilepredict.com/v2/api_how_to/)) — just the forecast entity.
   - *Custom* — the forecast entity plus attribute/key names and a unit multiplier.

Scheduling tolerances (gamble tolerance, min/max block hours, max price)
are **not** part of the config flow — they're live `number` entities (see
below) you can tweak day-to-day without going through Settings and reloading.

Adding support for another price source (Amber, Nordpool, ...) means adding
one entry to `providers.py` plus one config_flow step that mirrors the
existing `rates_octopus_energy` / `forecast_agile_predict` ones — "Custom"
already covers any source in the meantime.

**Set daily (entities created by the integration):**
- `datetime.<prefix>_ready_by` — when charging must be complete by
- `number.<prefix>_charging_hours_required` — hours needed; 0 = idle

**Live scheduling tolerances (adjust anytime, no reload needed):**
- `number.<prefix>_gamble_tolerance` — 0–100%; how much to trust predicted (non-actual) prices
- `number.<prefix>_min_block_hours` — minimum length of any single charging block
- `number.<prefix>_max_block_hours` — maximum length of any single charging block; 0 = unlimited. Caps how large one *window* can be during selection so separate cheap price dips get combined into several smaller blocks instead of one long one — see the `find_optimal_slots` docstring in `scheduler.py` for the nuance around what this does and doesn't guarantee (it's a cap on selection, not an enforced rest period)
- `number.<prefix>_max_price` — session-level average price ceiling

**Boost / stop / reset:**
- `number.<prefix>_boost_duration_hours` — set > 0 to start an immediate
  boost for that many hours; resets to 0 once registered
- `button.<prefix>_boost_cancel` — cancel an active boost early
- `button.<prefix>_stop` — clear today's schedule and any boost, but **keep**
  `ready_by` (useful if you've decided not to charge today but the same
  deadline applies tomorrow)
- `button.<prefix>_reset` — full reset: also clears `ready_by`. Intended to
  be wired to an automation that fires when the charger becomes unplugged,
  so the next plug-in starts from a completely clean slate. Scheduling
  tolerances (gamble tolerance, block hours, max price) are left untouched —
  those are standing preferences, not per-session state

## Outputs

**Primary (visible by default):**
- `sensor.<prefix>_charging_state` — idle / scheduled / charging / boosting / complete / unschedulable / error
- `sensor.<prefix>_charging_schedule` — `slots` attribute holds the full session list (every proposed/committed block, not just the next one), plus `future_slots`, `block_count`
- `sensor.<prefix>_next_slot_start` / `_next_slot_end`
- `sensor.<prefix>_hours_remaining` — plain decimal hours
- `sensor.<prefix>_time_remaining` — the same value as a proper `duration` device-class sensor (minutes), for nicer HA rendering ("1:30:00" instead of "1.5")
- `sensor.<prefix>_boost_ends_at` — timestamp of when an active boost ends; `unknown` outside of boosting
- `binary_sensor.<prefix>_charging_desired` — drive your charger switch from this

**Diagnostics (hidden by default — `entity_category: diagnostic`, still fully
available for automations/history/graphs, just not cluttering your
dashboard by default):**
- `sensor.<prefix>_block_count` — number of blocks in the current schedule
- `sensor.<prefix>_upcoming_block_2_start` / `_end`, `_upcoming_block_3_start` / `_end` — visibility into further blocks when the schedule has more than one (see `next_slot_start`/`_end` for the first)
- `sensor.<prefix>_candidate_price_points` — how many price data points were available this cycle ("options considered")
- `sensor.<prefix>_cheapest_available_price` / `_most_expensive_available_price` — range across the whole current candidate dataset, not just what got scheduled
- `sensor.<prefix>_average_price_next_24h` / `_average_price_all_data`
- `sensor.<prefix>_price_data_sources` — state is the total candidate count; `source_counts` attribute breaks it down by actual/predicted
- `sensor.<prefix>_active_providers` — which named provider (see providers.py) supplies rates and forecast data, for visibility without inspecting config

## Entity naming

`entity.py` sets each entity's `entity_id` explicitly (rather than letting HA
derive it from the friendly name) to `<platform>.<prefix>_<suffix>`, where
`prefix` is the slugified instance name chosen during setup (defaults to
`wholesale_ev_schedule` — see `const.DEFAULT_NAME`). The config flow enforces
uniqueness on that slug via the entry's `unique_id`, so two instances can
never end up sharing a prefix. `tests/test_entity_naming.py` and
`tests/test_multi_instance.py` cover both the prefix guarantee and the
no-collision-with-the-pyscript-original guarantee.

## Testing

All tests run inside Docker (`public.ecr.aws/docker/library/python:trixie`):

```bash
./run-tests.sh              # run tests
./run-tests.sh --coverage   # with an HTML coverage report
./run-tests.sh --shell      # interactive shell in the container
```

92 tests, 97% branch coverage (100% on `config_flow.py`, `providers.py`, `button.py`):
- `tests/test_scheduler.py` — unit tests for the pure scheduling algorithm, including max_block_hours splitting and the price-summary diagnostics
- `tests/test_integration_smoke.py` — full config/options flow, entity registration
- `tests/test_providers.py` — custom rates/forecast provider branches, "no forecast" branch
- `tests/test_multi_instance.py` — two instances with distinct names don't collide; duplicate-name/slug is rejected
- `tests/test_entity_naming.py` — entity_id prefix guarantee, no collision with the pyscript original
- `tests/test_price_parsing.py` — custom rate/forecast attribute & key names, unit multiplier, missing forecast entity, gamble tolerance, custom charger-connected states
- `tests/test_tolerances_and_reload.py` — max_price/min_block_hours/max_block_hours enforcement, options-triggered reload picking up a changed update interval
- `tests/test_boost_stop.py` — boost start/self-reset, boost cancel, stop vs reset, boost_ends_at visibility
- `tests/test_diagnostics.py` — diagnostic sensors are hidden by default, price-summary diagnostics populate even when idle, block/upcoming-block sensors, live tuning number entities
- `tests/test_edge_cases.py` — malformed price data, ready_by in the past, active-session persistence across a price refresh, natural boost expiry

## CI / releases

`.github/workflows/ci.yml` runs the Docker test suite on every push/PR to
`main`, and publishes a GitHub release (zipping `custom_components/wholesale_ev_schedule/`)
whenever `manifest.json`'s `version` changes and doesn't already have a
matching tag.
