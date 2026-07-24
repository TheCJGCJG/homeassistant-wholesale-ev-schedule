# Wholesale EV Schedule

A Home Assistant integration that works out the cheapest times to charge your
EV from a wholesale/half-hourly electricity tariff (e.g. Octopus Agile), and
tells you — via a simple on/off signal — when charging should happen.

> **Actively developed, and daily-driven.** I run this on my own car, every
> day — so if it doesn't charge, that's my problem too, and I'm about as
> motivated as anyone to keep it working. That said, it's still early,
> single-maintainer software, and options/entities occasionally get
> restructured between updates. Use it, but at your own risk — and read the
> release notes before upgrading an existing install.

## What it does

You tell it two things: **how many hours** of charging you need, and **by
when**. It watches your wholesale price data and works out the cheapest
combination of time slots that adds up to what you need — including,
optionally, splitting into several separate cheap windows rather than one
long block. It then exposes a single **"charging desired"** signal that you
wire into whatever actually controls your charger.

It doesn't talk to your charger directly and doesn't need to know what brand
it is — you connect the dots with a normal HA automation (see
[Connecting your charger](#connecting-your-charger) below).

## Why not just use Octopus's own target-rate feature?

Fair question, and worth being honest about: if all you've got is **known,
already-published** Octopus Agile rates, this integration isn't a big step up
from Octopus's own built-in target-rate/smart-charging tools — those already
pick the cheapest published half-hours perfectly well on their own.

Where this integration actually earns its keep is **forecasting** — scheduling
against *tomorrow's* prices hours before Octopus actually publishes them
(typically mid-afternoon for the next day). Point it at
**[AgilePredict](https://agilepredict.com/H/)** or
**[Agile Forecast](https://agileforecast.co.uk/J?range=7d)** — two free,
community-run Octopus Agile price prediction services — as the forecast
source in [Setup](#setup) below, and it'll plan across known *and* predicted
prices together, re-optimising as forecasts firm up and real rates get
published. Turning an AgilePredict or Agile Forecast prediction into an
actual charging schedule, not just a number on a dashboard, is the actual
point of this add-on.

Got your own prediction model, or a different forecast source entirely? Wire
it up via the "Custom" forecast option in setup — anything exposing a list of
predicted price points works.

## Requirements

- Home Assistant 2026.3 or later.
- A wholesale/half-hourly price source. Currently built in:
  - **[Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy)** integration (for actual, known rates)
  - **[AgilePredict](https://agilepredict.com/v2/api_how_to/)** or any API-compatible source (e.g. Agile Forecast) (for a price forecast beyond what's known yet — optional, improves scheduling further ahead)
  - Anything else can be wired up via the "Custom" option in setup — see [Setup](#setup) below.
- Some way to turn your charger on/off from Home Assistant (a smart switch,
  the charger's own HA integration, whatever you've already got).

## Installation

**Via [HACS](https://hacs.xyz/)** (recommended):

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=TheCJGCJG&repository=homeassistant-wholesale-ev-schedule&category=integration)

Or manually: HACS → Integrations → ⋮ → Custom repositories → add
`https://github.com/TheCJGCJG/homeassistant-wholesale-ev-schedule` as an
Integration.

**Manually, without HACS**: copy `custom_components/wholesale_ev_schedule/`
into your Home Assistant `config/custom_components/` directory.

Either way, restart Home Assistant afterward.

## Setup

Go to **Settings → Devices & services → Add Integration → Wholesale EV
Schedule**. You'll be asked for:

1. **A name** — just used to label the integration; if you're setting this up
   for more than one car, give each one a distinct name (e.g. "Tesla EV
   Schedule").
2. **How often to re-evaluate** — how frequently it re-checks prices and
   recomputes the schedule (5 minutes is a sensible default). Charging
   desired also re-evaluates every minute on the clock regardless of this
   setting, so on/off transitions always land within about a minute of the
   real slot boundary.
3. **Where your actual rates come from** — pick "Octopus Energy" and select
   your current-day and next-day rates entities (found under Settings →
   Devices & services → Octopus Energy), or "Custom" to point at a different
   source.
4. **Where your price forecast comes from** (optional) — pick
   "AgilePredict-compatible API" and select the forecast sensor you've set
   up (works with AgilePredict, Agile Forecast, or anything else exposing
   the same API shape — see the
   [AgilePredict HA guide](https://agilepredict.com/v2/api_how_to/)), "Custom"
   for something else, or "None" to schedule from known/actual rates only.
5. **Optional defaults** — default charging hours required, default gamble
   tolerance, default maximum price, default minimum block length, and a
   default ready-by (an hour, plus "Next day" / "Next day + 1" / "Next day +
   2" / "Next day + 3" for how many days out it should land). These don't
   affect the day-to-day live values described below directly — they only
   set what a brand-new install starts with and what the **Reset** button
   restores those live values to. Leave them alone for the same behaviour as
   before (12h / 50% / 20 / 4h / next 7am). All are editable later from the
   integration's **Configure** option.

That's it for one-time setup. Everything else — how many hours you need,
by when, and the scheduling tolerances below — is adjusted afterwards as
live entities, no reconfiguration needed.

## Daily use

**Set these day-to-day** (both come with defaults so the integration does
something sensible out of the box — see below):
- **Ready by** (`datetime`) — when charging needs to be finished by. Defaults
  to the next 7am (configurable at setup time — see [Setup](#setup)), and
  **auto-rolls forward** to the same default hour/day once reached — it's a
  standing daily target, not something you need to reset every day. Set it
  manually any time you need a different deadline.
- **Charging hours required** (`number`) — how many hours of charging you
  need. Defaults to 12 (configurable at setup time). Set to 0 to go idle.

**Tune scheduling behaviour anytime** (these have sensible defaults,
configurable at setup time, most people won't need to touch them):
- **Gamble tolerance** (default 50%) — how much to trust predicted
  (not-yet-known) prices vs. discounting them in favour of known rates.
  Higher = more willing to bet on forecasts.
- **Minimum block length** (default 4h, 0–24h) — won't schedule a charging
  session shorter than this, to avoid rapidly switching your charger on and
  off. Set to 0 for no minimum at all.
- **Maximum price** (default 20) — won't schedule anything above this
  average price per session, even if it means missing your target hours.
  This is a plain number, not currency-aware — it's compared directly
  against your price data after your configured unit multiplier is applied
  (e.g. pence/kWh for a typical UK £/kWh source with the default 100×
  multiplier). Set it to match whatever unit your prices actually end up in.
- **Assumed charge kWh** (default 7) — a rough stand-in for how much energy a
  session actually delivers, since this integration only ever knows time
  slots and price, never real delivered kWh. Used purely to compute the
  estimated cost sensor below — set it to roughly match your car/charger for
  a more useful number.

**Boost, stop, and reset:**
- **Boost duration** (`number`) — set this to a number of hours to start
  charging immediately for that long, ignoring the schedule. Resets to 0
  automatically once it takes effect.
- **Cancel boost** (button) — end an active boost early.
- **Stop** (button) — kills/ends the current session: clears today's
  schedule and cancels any boost, but **keeps** your ready-by time and every
  tuning preference untouched (useful if you've decided not to charge today
  but the same deadline still applies tomorrow).
- **Reset** (button) — puts everything back to defaults: ready-by, hours
  required, gamble tolerance, minimum block length, max price, assumed
  charge kWh, the charge override, and the optimization algorithm, on top of
  clearing the schedule and any boost like Stop does. Handy to wire to an
  automation that fires when your charger becomes unplugged, so the next
  time you plug in you're starting completely fresh. Which values Reset
  restores (other than assumed charge kWh, the charge override, and the
  optimization algorithm, which are fixed) is controlled by the optional
  defaults set at setup time — see [Setup](#setup) above.

**Manual override:**
- **Charge override** (`select`: Auto / Force On / Force Off) — leave on
  "Auto" for normal scheduled behaviour. Switch to "Force On" or "Force Off"
  to manually override the charging signal regardless of what the schedule
  says — useful for "just charge now, I don't care about price" or "don't
  charge no matter what, even if it's a cheap slot" (e.g. the car's in for
  service).

**Optimization algorithm** (`select`: Greedy / Optimal / Hybrid, default
Greedy) — which strategy picks the cheapest charging slots:
- **Greedy** (default) — picks the single cheapest window first and fills
  any remainder from what's left. Fast, but can occasionally miss a
  cheaper combination — e.g. two separate windows that individually look
  cheap can add up to more than one larger window that spans both, which
  greedy never compares against.
- **Optimal** — an exact search that always finds the true cheapest
  combination, never worse than Greedy. More compute on a long price
  horizon combined with a large or unlimited minimum block length, though
  still well under a second at realistic scale.
- **Hybrid** — a narrowed exact search over a handful of representative
  block sizes per price run. Close to Greedy's speed, much less likely to
  miss a cheaper single window, but — being a narrowed search rather than
  an exhaustive one — not guaranteed to find the true optimum the way
  Optimal is.

If you've noticed a scheduled session that looks like it's leaving cheaper
prices on the table, switching to Optimal (or Hybrid, if Optimal feels too
slow to compute) is the fix.

## What you'll see

- **State** — idle / scheduled / charging / boosting / complete /
  unschedulable / error, at a glance.
- **Schedule** — the full list of upcoming charging sessions, with each
  session's start/end time, average price, and confidence.
- **Next slot start / end** — when the next charging session begins and ends.
- **Next slot average price** / **Next slot estimated cost** — the average
  price of that upcoming session, and average price × assumed charge kWh as
  a rough total cost estimate (same currency-agnostic caveat as max price —
  see above).
- **Hours remaining** / **Time remaining** — how much committed charging time
  is left.
- **Charging desired** (`binary_sensor`) — the actual on/off signal; see
  below for wiring this to your charger.

There's also a set of hidden diagnostic sensors (block count, further
upcoming blocks, price ranges/averages, which data sources are active) —
they're not shown on your dashboard by default, but are there for automations
or if you want to add them yourself. Look under the integration's device page
→ "Diagnostic" section to find and enable them.

## Connecting your charger

`binary_sensor.<prefix>_charging_desired` turns `on` when charging should be
happening and `off` when it shouldn't (accounting for the schedule, any
boost, and the manual override). Wire it to your charger with a simple
automation, for example:

```yaml
automation:
  - alias: "EV charger follow desired state"
    trigger:
      - platform: state
        entity_id: binary_sensor.wholesale_ev_schedule_charging_desired
    action:
      - service: >-
          switch.turn_{{ 'on' if trigger.to_state.state == 'on' else 'off' }}
        target:
          entity_id: switch.my_ev_charger
```

Replace `switch.my_ev_charger` with whatever actually controls your charger —
a smart plug, a switch exposed by the charger's own integration, or a script
that calls an API. This integration doesn't need to know anything about your
charger's own state to work.

If your charger or vehicle has its own HA integration (e.g. OCPP, or a
vehicle-brand integration like Tesla/Polestar/etc.), you'll often get better
results calling *its* start/stop-charging service instead of a plain switch —
same trigger on `charging_desired`, different `action`. Either way,
`charging_desired` turning `off` mid-session — whether because the schedule
finished, you hit [Stop](#daily-use), or you set the
[charge override](#daily-use) to Force Off — is exactly the signal that
should stop the physical charge; there's nothing extra to wire up for that.

## What this doesn't do for you

This integration only ever computes **when** to charge and exposes that as
`charging_desired` plus the tuning entities in [Daily use](#daily-use). It
deliberately doesn't know anything about your specific charger or car:
starting/stopping the physical charge, reading battery %, or tracking session
state (plugged in, paused, complete). Charger and vehicle integrations vary
too much between brands to model that generically here, so those bits are
left to your own automations. The two most common gaps to fill:

**Setting ready-by.** Everyone's routine is different, and when the car gets
plugged in on a given day changes what "ready by" should mean — this isn't
something the integration can guess. The built-in default (next 7am,
auto-rolling — see [Daily use](#daily-use)) already covers the common "same
time every day" case with zero automation needed. Only automate this if your
ready-by genuinely varies, e.g.:

```yaml
automation:
  - alias: "Set EV ready-by from tomorrow's calendar event"
    trigger:
      - platform: state
        entity_id: binary_sensor.car_plugged_in
        to: "on"
    action:
      - service: datetime.set_value
        target:
          entity_id: datetime.wholesale_ev_schedule_ready_by
        data:
          datetime: "{{ states('input_datetime.next_departure') }}"
```

Trigger on whatever signals "the car just got plugged in" for your setup, and
source the target time from wherever you already track it — a calendar
entity, an `input_datetime` you maintain yourself, or (for a genuinely fixed
routine) a literal time.

**Setting charging hours required.** As the issue that prompted this section
puts it, this logic is too vehicle-specific to build in. If your vehicle's HA
integration exposes something usable — target charge %, current battery %, or
an estimated "time to target" sensor — prefer wiring an automation off that
over guessing:

```yaml
automation:
  - alias: "Set EV charging hours from battery %"
    trigger:
      - platform: state
        entity_id: binary_sensor.car_plugged_in
        to: "on"
    action:
      - service: number.set_value
        target:
          entity_id: number.wholesale_ev_schedule_charging_hours_required
        data:
          value: >-
            {{ ((80 - states('sensor.car_battery_percent') | float)
                / 100 * 60 / 7) | round(1) }}
```

That example estimates hours as `(target% - current%) / 100 * battery_kWh /
charger_kW` — adjust the numbers (target percentage, battery capacity,
charger power) to match your car and charger. If your vehicle doesn't expose
anything usable, a rough manual or static number is a reasonable fallback —
this integration intentionally doesn't attempt this calculation itself, since
it varies too much by vehicle/charger combination. Both examples above are
illustrative patterns, not copy-paste-ready automations — the exact trigger
and data source are unavoidably specific to your setup.

## Multiple cars

Set up the integration again with a different name — each instance gets its
own set of entities prefixed with its own slugified name (e.g.
`number.tesla_ev_schedule_charging_hours_required` vs.
`number.polestar_ev_schedule_charging_hours_required`), so they run
side by side without clashing.

## Contributing / technical details

See [DEVELOPING.md](DEVELOPING.md) for the file structure, architecture
notes, how to add a new price provider, and how to run the test suite. If
you're an AI coding agent, start at [AGENTS.md](AGENTS.md) instead.
