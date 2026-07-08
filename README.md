# Wholesale EV Schedule

> **⚠️ Not ready for use.** This is a work-in-progress port and has not been
> run against a live Home Assistant instance or real price/charger entities —
> only against the automated test suite described below. Do not install this
> on a production HA setup yet. Boost mode, the stop button, multi-instance
> support, and the provider-based price parsing are implemented but
> unverified in practice. Treat this repo as a draft until this notice is
> removed.

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
  config_flow.py    multi-step setup + options: name, providers, then tolerances
  entity.py         shared base — forces the <prefix>_ entity_id namespace
  sensor.py         state, schedule, next-slot, hours-remaining outputs
  binary_sensor.py  charging_desired output
  number.py         charging_hours_required, boost_duration_hours — live inputs
  datetime.py       ready_by — live input
  button.py         boost_cancel, stop — actions
```

## Configuration

The config flow (and its matching **Configure** options flow) walks through:

1. **Name + charger + providers** — a name for this instance (becomes the
   entity_id prefix; give each instance a distinct one if running more than
   one), the charger work-state sensor and its "connected" states, and which
   named provider supplies your actual and forecast wholesale prices.
2. **Rates step** (shape depends on the provider chosen in step 1):
   - *Octopus Energy* — just the current-day and next-day rates event entities.
   - *Custom* — the same two entities, plus the attribute/key names and a unit
     multiplier, for a source not modelled in `providers.py`.
3. **Forecast step** (skipped entirely if you pick "None"):
   - *AgilePredict* ([agilepredict.com](https://agilepredict.com/v2/api_how_to/)) — just the forecast entity.
   - *Custom* — the forecast entity plus attribute/key names and a unit multiplier.
4. **Scheduling tolerances** — gamble tolerance, min block hours, max price,
   and how often to re-evaluate (minutes).

Adding support for another price source (Amber, Nordpool, ...) means adding
one entry to `providers.py` plus one config_flow step that mirrors the
existing `rates_octopus_energy` / `forecast_agile_predict` ones — "Custom"
already covers any source in the meantime.

**Set daily (entities created by the integration):**
- `datetime.<prefix>_ready_by` — when charging must be complete by
- `number.<prefix>_charging_hours_required` — hours needed; 0 = idle

**Boost / stop:**
- `number.<prefix>_boost_duration_hours` — set > 0 to start an immediate
  boost for that many hours; resets to 0 once registered
- `button.<prefix>_boost_cancel` — cancel an active boost early
- `button.<prefix>_stop` — clear the whole schedule and any boost

## Outputs

- `sensor.<prefix>_charging_state` — idle / scheduled / charging / boosting / complete / unschedulable / error
- `sensor.<prefix>_charging_schedule` — `slots` attribute holds the full session list
- `sensor.<prefix>_next_slot_start` / `_next_slot_end`
- `sensor.<prefix>_hours_remaining`
- `binary_sensor.<prefix>_charging_desired` — drive your charger switch from this

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

70 tests, 97% coverage (100% on `config_flow.py` and `providers.py`):
- `tests/test_scheduler.py` — unit tests for the pure scheduling algorithm
- `tests/test_integration_smoke.py` — full config/options flow, entity registration
- `tests/test_providers.py` — custom rates/forecast provider branches, "no forecast" branch
- `tests/test_multi_instance.py` — two instances with distinct names don't collide; duplicate-name/slug is rejected
- `tests/test_entity_naming.py` — entity_id prefix guarantee, no collision with the pyscript original
- `tests/test_price_parsing.py` — custom rate/forecast attribute & key names, unit multiplier, missing forecast entity, gamble tolerance, custom charger-connected states
- `tests/test_tolerances_and_reload.py` — max_price/min_block_hours enforcement, options-triggered reload picking up a changed update interval
- `tests/test_boost_stop.py` — boost start/self-reset, boost cancel, stop
- `tests/test_edge_cases.py` — malformed price data, ready_by in the past, active-session persistence across a price refresh, natural boost expiry
