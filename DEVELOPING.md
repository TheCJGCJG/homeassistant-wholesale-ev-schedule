# Developing Wholesale EV Schedule

Technical/contributor documentation. For what the integration does and how
to set it up, see [README.md](README.md).

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
  select.py         charge_override — manual force-on/force-off
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
- **`max_block_hours` caps window *selection* size, not an enforced rest
  period.** If the single cheapest option in the market genuinely is one long
  uninterrupted dip, capping the window size still produces one long block —
  see the docstring on `find_optimal_slots` in `scheduler.py` for the full
  reasoning, and `tests/test_scheduler.py::test_find_optimal_slots_caps_individual_window_size_with_max_block_hours`
  for a worked example of what it does and doesn't guarantee.

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

96 tests, 97% branch coverage (100% on `config_flow.py`, `providers.py`,
`button.py`, `select.py`):
- `tests/test_scheduler.py` — unit tests for the pure scheduling algorithm, including max_block_hours splitting and the price-summary diagnostics
- `tests/test_integration_smoke.py` — full config/options flow, entity registration
- `tests/test_providers.py` — custom rates/forecast provider branches, "no forecast" branch
- `tests/test_multi_instance.py` — two instances with distinct names don't collide; duplicate-name/slug is rejected
- `tests/test_entity_naming.py` — entity_id prefix guarantee, no collision with the pyscript original
- `tests/test_price_parsing.py` — custom rate/forecast attribute & key names, unit multiplier, missing forecast entity, gamble tolerance
- `tests/test_charge_override.py` — auto/force-on/force-off, including overriding an active slot or boost, and persistence across restarts
- `tests/test_tolerances_and_reload.py` — max_price/min_block_hours/max_block_hours enforcement, options-triggered reload picking up a changed update interval
- `tests/test_boost_stop.py` — boost start/self-reset, boost cancel, stop vs reset, boost_ends_at visibility
- `tests/test_diagnostics.py` — diagnostic sensors are hidden by default, price-summary diagnostics populate even when idle, block/upcoming-block sensors, live tuning number entities
- `tests/test_edge_cases.py` — malformed price data, ready_by in the past, active-session persistence across a price refresh, natural boost expiry

## CI / releases

`.github/workflows/ci.yml` runs the Docker test suite on every push/PR to
`main`, and publishes a GitHub release (zipping `custom_components/wholesale_ev_schedule/`)
whenever `manifest.json`'s `version` changes and doesn't already have a
matching tag — so bumping the version is what triggers a release, not every
commit.

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
