# Wholesale EV Schedule

> **⚠️ Not ready for use.** This is a work-in-progress port and has not been
> run against a live Home Assistant instance or real price/charger entities —
> only against the automated test suite described below. Do not install this
> on a production HA setup yet. Boost mode, the stop button, and the
> configurable price-parsing options are implemented but unverified in
> practice. Treat this repo as a draft until this notice is removed.

A Home Assistant custom integration that schedules EV charging across the
cheapest wholesale price windows. Ported from the scheduling algorithm in
[homeassistant-pyscripts](https://github.com/TheCJGCJG/homeassistant-pyscripts)
into a proper integration with a config flow for its inputs and `sensor`/
`binary_sensor`/`number`/`datetime`/`button` entities for its outputs, instead
of pyscript + `input_*` helpers.

Designed to run **alongside** an existing pyscript-based (or any other)
EV-charging setup on the same HA instance: every entity_id this integration
creates is forced to start with `wholesale_ev_schedule_` (see "Entity naming"
below), so it can never collide with another integration's entities.

## Structure

```
custom_components/wholesale_ev_schedule/
  scheduler.py      pure scheduling algorithm, no HA dependency
  coordinator.py    reads price/charger entities, runs scheduler.py, persists state
  config_flow.py    two-step setup + options: entity wiring, then tolerances/parsing
  entity.py         shared base — forces the wholesale_ev_schedule_ entity_id prefix
  sensor.py         state, schedule, next-slot, hours-remaining outputs
  binary_sensor.py  charging_desired output
  number.py         charging_hours_required, boost_duration_hours — live inputs
  datetime.py       ready_by — live input
  button.py         boost_cancel, stop — actions
```

## Configuration

The config flow (and its matching **Configure** options flow) has two steps:

**Step 1 — entity wiring:** current-day rates entity, next-day rates entity,
optional price forecast entity, charger work-state sensor, and the charger
states that mean "connected".

**Step 2 — tolerances and price parsing:** gamble tolerance, min block hours,
max price, how often to re-evaluate (minutes), and the price-entity parsing
overrides (attribute/key names + a unit multiplier) needed to point this at a
wholesale price source shaped differently than Octopus Energy's event
entities — Amber, Nordpool, a template sensor, etc.

**Set daily (entities created by the integration):**
- `datetime.wholesale_ev_schedule_ready_by` — when charging must be complete by
- `number.wholesale_ev_schedule_charging_hours_required` — hours needed; 0 = idle

**Boost / stop:**
- `number.wholesale_ev_schedule_boost_duration_hours` — set > 0 to start an
  immediate boost for that many hours; resets to 0 once registered
- `button.wholesale_ev_schedule_boost_cancel` — cancel an active boost early
- `button.wholesale_ev_schedule_stop` — clear the whole schedule and any boost

## Outputs

- `sensor.wholesale_ev_schedule_charging_state` — idle / scheduled / charging / boosting / complete / unschedulable / error
- `sensor.wholesale_ev_schedule_charging_schedule` — `slots` attribute holds the full session list
- `sensor.wholesale_ev_schedule_next_slot_start` / `_next_slot_end`
- `sensor.wholesale_ev_schedule_hours_remaining`
- `binary_sensor.wholesale_ev_schedule_charging_desired` — drive your charger switch from this

## Entity naming

`entity.py` sets each entity's `entity_id` explicitly (rather than letting HA
derive it from the friendly name) to `<platform>.wholesale_ev_schedule_<suffix>`,
guaranteed regardless of translation loading or naming collisions. `tests/test_entity_naming.py`
asserts every entity_id carries this prefix and that none collide with the
original pyscript's entity_ids (`sensor.ev_charging_state`, etc).

## Testing

All tests run inside Docker (`public.ecr.aws/docker/library/python:trixie`):

```bash
./run-tests.sh              # run tests
./run-tests.sh --coverage   # with an HTML coverage report
./run-tests.sh --shell      # interactive shell in the container
```

63 tests, 97% coverage:
- `tests/test_scheduler.py` — unit tests for the pure scheduling algorithm
- `tests/test_integration_smoke.py` — two-step config/options flow, entity registration
- `tests/test_entity_naming.py` — entity_id prefix guarantee, no collision with the pyscript original
- `tests/test_price_parsing.py` — custom rate/forecast attribute & key names, unit multiplier, missing forecast entity, gamble tolerance, custom charger-connected states
- `tests/test_tolerances_and_reload.py` — max_price/min_block_hours enforcement, options-triggered reload picking up a changed update interval
- `tests/test_boost_stop.py` — boost start/self-reset, boost cancel, stop
- `tests/test_edge_cases.py` — malformed price data, ready_by in the past, active-session persistence across a price refresh, natural boost expiry
