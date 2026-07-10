"""Config flow for Wholesale EV Schedule.

Steps: user/init (name + poll interval + pick a rates/forecast provider) -> a
provider-specific rates step -> a provider-specific forecast step (or none).
Picking a named provider (see providers.py) fills in its attribute/key shape
automatically; picking "custom" asks for it directly, so an unmodelled
wholesale price source can still be wired up.

Scheduling tolerances (gamble tolerance, min/max block hours, max price) and
the manual charge_override deliberately aren't here as *live* values — they're
`number`/`select` entities (see coordinator.py, number.py, select.py) since
they're the kind of thing you adjust day-to-day, not a one-time setup choice.
`base_schema` does include a matching set of *default* fields (e.g.
default_gamble_tolerance) — these only control what a fresh install starts
with and what those live entities reset back to when "Reset" is pressed; they
never change a live value directly (see coordinator.py). There's also
deliberately no "charger state entity" wiring: charging_desired is computed
purely from the schedule and the manual override, independent of any
particular charger's own state, so this integration doesn't need to know
anything about your charger brand.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_CURRENT_RATES_ENTITY,
    CONF_DEFAULT_GAMBLE_TOLERANCE,
    CONF_DEFAULT_MAX_PRICE,
    CONF_DEFAULT_MIN_BLOCK_HOURS,
    CONF_DEFAULT_READY_BY_DAY_OFFSET,
    CONF_DEFAULT_READY_BY_HOUR,
    CONF_DEFAULT_REQUIRED_HOURS,
    CONF_FORECAST_ATTRIBUTE,
    CONF_FORECAST_DATETIME_KEY,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PRICE_KEY,
    CONF_FORECAST_PROVIDER,
    CONF_FORECAST_UNIT_MULTIPLIER,
    CONF_NEXT_RATES_ENTITY,
    CONF_RATE_START_KEY,
    CONF_RATE_UNIT_MULTIPLIER,
    CONF_RATE_VALUE_KEY,
    CONF_RATES_ATTRIBUTE,
    CONF_RATES_PROVIDER,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_GAMBLE_TOLERANCE,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_BLOCK_HOURS,
    DEFAULT_NAME,
    DEFAULT_RATE_UNIT_MULTIPLIER,
    DEFAULT_READY_BY_DAY_OFFSET,
    DEFAULT_READY_BY_HOUR,
    DEFAULT_REQUIRED_HOURS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .providers import (
    FORECAST_PROVIDER_AGILE_PREDICT,
    FORECAST_PROVIDER_CUSTOM,
    FORECAST_PROVIDER_NONE,
    FORECAST_PROVIDERS,
    RATE_PROVIDER_CUSTOM,
    RATE_PROVIDER_OCTOPUS_ENERGY,
    RATE_PROVIDERS,
)


def _required(key: str, defaults: dict[str, Any]) -> vol.Required:
    """vol.Required with a default only when one actually exists — an entity
    selector rejects a `None` default that voluptuous would otherwise insert for
    any key the user leaves blank."""
    if key in defaults:
        return vol.Required(key, default=defaults[key])
    return vol.Required(key)


def _with_default(key: str, defaults: dict[str, Any], fallback: Any) -> vol.Required:
    return vol.Required(key, default=defaults.get(key, fallback))


def _provider_select(options: dict[str, dict]) -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=[selector.SelectOptionDict(value=k, label=v["label"]) for k, v in options.items()],
        mode=selector.SelectSelectorMode.DROPDOWN,
    ))


# "Next day" (0) / "Next day + 1/2/3" — how many days ahead of "as soon as
# possible" the default ready_by should be pushed. See
# CONF_DEFAULT_READY_BY_DAY_OFFSET in const.py and scheduler.next_ready_by.
# Option labels live in strings.json/translations under
# selector.default_ready_by_day_offset.options.
_READY_BY_DAY_OFFSET_SELECT = selector.SelectSelector(selector.SelectSelectorConfig(
    options=["0", "1", "2", "3"],
    mode=selector.SelectSelectorMode.DROPDOWN,
    translation_key="default_ready_by_day_offset",
))


def base_schema(defaults: dict[str, Any], include_name: bool) -> vol.Schema:
    schema: dict[Any, Any] = {}
    if include_name:
        # Slugified into the entity_id prefix (e.g. "Tesla EV Schedule" ->
        # entities named number.tesla_ev_schedule_*) so multiple instances of
        # this integration can run side by side without colliding.
        schema[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME))] = str
    schema.update({
        _with_default(
            CONF_UPDATE_INTERVAL_MINUTES, defaults, DEFAULT_UPDATE_INTERVAL_MINUTES
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        _with_default(CONF_RATES_PROVIDER, defaults, RATE_PROVIDER_OCTOPUS_ENERGY): _provider_select(RATE_PROVIDERS),
        _with_default(
            CONF_FORECAST_PROVIDER, defaults, FORECAST_PROVIDER_AGILE_PREDICT
        ): _provider_select(FORECAST_PROVIDERS),
        # Setup-time defaults: what a fresh install starts with and what the
        # "Reset" button restores every live value to — not the live values
        # themselves (see coordinator.py).
        _with_default(
            CONF_DEFAULT_REQUIRED_HOURS, defaults, DEFAULT_REQUIRED_HOURS
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=24, step=0.5, mode=selector.NumberSelectorMode.BOX)
        ),
        _with_default(
            CONF_DEFAULT_GAMBLE_TOLERANCE, defaults, DEFAULT_GAMBLE_TOLERANCE
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=100, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        _with_default(CONF_DEFAULT_MAX_PRICE, defaults, DEFAULT_MAX_PRICE): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=200, step=0.1, mode=selector.NumberSelectorMode.BOX)
        ),
        _with_default(
            CONF_DEFAULT_MIN_BLOCK_HOURS, defaults, DEFAULT_MIN_BLOCK_HOURS
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=24, step=0.5, mode=selector.NumberSelectorMode.BOX)
        ),
        _with_default(CONF_DEFAULT_READY_BY_HOUR, defaults, DEFAULT_READY_BY_HOUR): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required(
            CONF_DEFAULT_READY_BY_DAY_OFFSET,
            default=str(defaults.get(CONF_DEFAULT_READY_BY_DAY_OFFSET, DEFAULT_READY_BY_DAY_OFFSET)),
        ): _READY_BY_DAY_OFFSET_SELECT,
    })
    return vol.Schema(schema)


def rates_octopus_energy_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        _required(CONF_CURRENT_RATES_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="event")
        ),
        _required(CONF_NEXT_RATES_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="event")
        ),
    })


def rates_custom_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        _required(CONF_CURRENT_RATES_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="event")
        ),
        _required(CONF_NEXT_RATES_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="event")
        ),
        _with_default(CONF_RATES_ATTRIBUTE, defaults, "rates"): str,
        _with_default(CONF_RATE_START_KEY, defaults, "start"): str,
        _with_default(CONF_RATE_VALUE_KEY, defaults, "value_inc_vat"): str,
        _with_default(
            CONF_RATE_UNIT_MULTIPLIER, defaults, DEFAULT_RATE_UNIT_MULTIPLIER
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.01, max=1000, step=0.01, mode=selector.NumberSelectorMode.BOX)
        ),
    })


def forecast_agile_predict_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        _required(CONF_FORECAST_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
    })


def forecast_custom_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        _required(CONF_FORECAST_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        _with_default(CONF_FORECAST_ATTRIBUTE, defaults, "prices"): str,
        _with_default(CONF_FORECAST_DATETIME_KEY, defaults, "date_time"): str,
        _with_default(CONF_FORECAST_PRICE_KEY, defaults, "agile_pred"): str,
        _with_default(CONF_FORECAST_UNIT_MULTIPLIER, defaults, 1.0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.01, max=1000, step=0.01, mode=selector.NumberSelectorMode.BOX)
        ),
    })


class _ProviderStepsMixin:
    """Steps shared by the initial config flow and the options flow, from the
    rates-provider branch through to the forecast step. Subclasses implement
    `_async_finish` to actually create/update the entry.
    """

    def _provider_state(self) -> dict[str, Any]:
        if not hasattr(self, "_provider_options"):
            self._provider_options: dict[str, Any] = {}
        return self._provider_options

    async def _async_after_base_step(self, base_options: dict[str, Any]):
        state = self._provider_state()
        state.update(base_options)
        if state[CONF_RATES_PROVIDER] == RATE_PROVIDER_CUSTOM:
            return await self.async_step_rates_custom()
        return await self.async_step_rates_octopus_energy()

    async def async_step_rates_octopus_energy(self, user_input: dict[str, Any] | None = None):
        state = self._provider_state()
        if user_input is not None:
            profile = RATE_PROVIDERS[RATE_PROVIDER_OCTOPUS_ENERGY]
            state.update(user_input)
            state[CONF_RATES_ATTRIBUTE] = profile["attribute"]
            state[CONF_RATE_START_KEY] = profile["start_key"]
            state[CONF_RATE_VALUE_KEY] = profile["value_key"]
            state[CONF_RATE_UNIT_MULTIPLIER] = profile["unit_multiplier"]
            return await self._async_after_rates_step()

        return self.async_show_form(
            step_id="rates_octopus_energy", data_schema=rates_octopus_energy_schema(state)
        )

    async def async_step_rates_custom(self, user_input: dict[str, Any] | None = None):
        state = self._provider_state()
        if user_input is not None:
            state.update(user_input)
            return await self._async_after_rates_step()

        return self.async_show_form(step_id="rates_custom", data_schema=rates_custom_schema(state))

    async def _async_after_rates_step(self):
        state = self._provider_state()
        forecast_provider = state[CONF_FORECAST_PROVIDER]
        if forecast_provider == FORECAST_PROVIDER_NONE:
            state[CONF_FORECAST_ENTITY] = None
            return await self._async_finish(state)
        if forecast_provider == FORECAST_PROVIDER_CUSTOM:
            return await self.async_step_forecast_custom()
        return await self.async_step_forecast_agile_predict()

    async def async_step_forecast_agile_predict(self, user_input: dict[str, Any] | None = None):
        state = self._provider_state()
        if user_input is not None:
            profile = FORECAST_PROVIDERS[FORECAST_PROVIDER_AGILE_PREDICT]
            state.update(user_input)
            state[CONF_FORECAST_ATTRIBUTE] = profile["attribute"]
            state[CONF_FORECAST_DATETIME_KEY] = profile["datetime_key"]
            state[CONF_FORECAST_PRICE_KEY] = profile["price_key"]
            state[CONF_FORECAST_UNIT_MULTIPLIER] = profile["unit_multiplier"]
            return await self._async_finish(state)

        return self.async_show_form(
            step_id="forecast_agile_predict", data_schema=forecast_agile_predict_schema(state)
        )

    async def async_step_forecast_custom(self, user_input: dict[str, Any] | None = None):
        state = self._provider_state()
        if user_input is not None:
            state.update(user_input)
            return await self._async_finish(state)

        return self.async_show_form(step_id="forecast_custom", data_schema=forecast_custom_schema(state))


class WholesaleEvScheduleConfigFlow(_ProviderStepsMixin, ConfigFlow, domain=DOMAIN):
    """Handle initial setup of the integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._name: str = DEFAULT_NAME

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            name = user_input.pop(CONF_NAME)
            # The slugified name becomes the entity_id prefix — enforce
            # uniqueness so two instances can never collide on entity_ids.
            await self.async_set_unique_id(slugify(name))
            self._abort_if_unique_id_configured()

            self._name = name
            return await self._async_after_base_step(user_input)

        return self.async_show_form(step_id="user", data_schema=base_schema({}, include_name=True))

    async def _async_finish(self, options: dict[str, Any]):
        return self.async_create_entry(title=self._name, data={CONF_NAME: self._name}, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WholesaleEvScheduleOptionsFlow()


class WholesaleEvScheduleOptionsFlow(_ProviderStepsMixin, OptionsFlow):
    """Edit the entity wiring, provider choice, and poll interval after setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        state = self._provider_state()
        if not state:
            state.update(dict(self.config_entry.options))

        if user_input is not None:
            return await self._async_after_base_step(user_input)

        return self.async_show_form(step_id="init", data_schema=base_schema(state, include_name=False))

    async def _async_finish(self, options: dict[str, Any]):
        return self.async_create_entry(title="", data=options)
