# Wholesale EV Schedule

A Home Assistant integration that works out the cheapest times to charge your
EV from a wholesale/half-hourly electricity tariff (e.g. Octopus Agile), and
tells you — via a simple on/off signal — when charging should happen.

> **⚠️ Still evolving.** This has been run against a live Home Assistant
> instance and produces real schedules, but it's under active development —
> options and entities occasionally get restructured between updates. Read
> the release notes before upgrading an existing install.

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

## Requirements

- Home Assistant 2026.3 or later.
- A wholesale/half-hourly price source. Currently built in:
  - **[Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy)** integration (for actual, known rates)
  - **[AgilePredict](https://agilepredict.com/v2/api_how_to/)** or any API-compatible source (e.g. Agile Forecast) (for a price forecast beyond what's known yet — optional, improves scheduling further ahead)
  - Anything else can be wired up via the "Custom" option in setup — see [Setup](#setup) below.
- Some way to turn your charger on/off from Home Assistant (a smart switch,
  the charger's own HA integration, whatever you've already got).

## Installation

Copy `custom_components/wholesale_ev_schedule/` into your Home Assistant
`config/custom_components/` directory, then restart Home Assistant.

## Setup

Go to **Settings → Devices & services → Add Integration → Wholesale EV
Schedule**. You'll be asked for:

1. **A name** — just used to label the integration; if you're setting this up
   for more than one car, give each one a distinct name (e.g. "Tesla EV
   Schedule").
2. **How often to re-evaluate** — how frequently it re-checks prices and
   recomputes the schedule (5 minutes is a sensible default).
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

That's it for one-time setup. Everything else — how many hours you need,
by when, and the scheduling tolerances below — is adjusted afterwards as
live entities, no reconfiguration needed.

## Daily use

**Set these day-to-day** (both come with defaults so the integration does
something sensible out of the box — see below):
- **Ready by** (`datetime`) — when charging needs to be finished by. Defaults
  to the next 7am, and **auto-rolls forward** to the following 7am once
  reached — it's a standing daily target, not something you need to reset
  every day. Set it manually any time you need a different deadline.
- **Charging hours required** (`number`) — how many hours of charging you
  need. Defaults to 12. Set to 0 to go idle.

**Tune scheduling behaviour anytime** (these have sensible defaults, most
people won't need to touch them):
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
  charge kWh, and the charge override, on top of clearing the schedule and
  any boost like Stop does. Handy to wire to an automation that fires when
  your charger becomes unplugged, so the next time you plug in you're
  starting completely fresh.

**Manual override:**
- **Charge override** (`select`: Auto / Force On / Force Off) — leave on
  "Auto" for normal scheduled behaviour. Switch to "Force On" or "Force Off"
  to manually override the charging signal regardless of what the schedule
  says — useful for "just charge now, I don't care about price" or "don't
  charge no matter what, even if it's a cheap slot" (e.g. the car's in for
  service).

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

## Multiple cars

Set up the integration again with a different name — each instance gets its
own set of entities prefixed with its own slugified name (e.g.
`number.tesla_ev_schedule_charging_hours_required` vs.
`number.polestar_ev_schedule_charging_hours_required`), so they run
side by side without clashing.

## Contributing / technical details

See [DEVELOPING.md](DEVELOPING.md) for the file structure, architecture
notes, how to add a new price provider, and how to run the test suite.
