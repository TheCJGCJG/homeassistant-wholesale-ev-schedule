"""Config flow for Wholesale EV Schedule.

Split into two steps so the form isn't overwhelming: "user"/"init" wires up the
entities that must exist for the integration to do anything, "advanced" holds
the scheduling tolerances and the price-entity parsing overrides that let this
point at wholesale price sources other than Octopus Energy's event-entity shape.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CHARGER_CONNECTED_STATES,
    CONF_CHARGER_STATE_ENTITY,
    CONF_CURRENT_RATES_ENTITY,
    CONF_FORECAST_ATTRIBUTE,
    CONF_FORECAST_DATETIME_KEY,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PRICE_KEY,
    CONF_GAMBLE_TOLERANCE,
    CONF_MAX_PRICE,
    CONF_MIN_BLOCK_HOURS,
    CONF_NEXT_RATES_ENTITY,
    CONF_RATE_START_KEY,
    CONF_RATE_UNIT_MULTIPLIER,
    CONF_RATE_VALUE_KEY,
    CONF_RATES_ATTRIBUTE,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_CHARGER_CONNECTED_STATES,
    DEFAULT_FORECAST_ATTRIBUTE,
    DEFAULT_FORECAST_DATETIME_KEY,
    DEFAULT_FORECAST_PRICE_KEY,
    DEFAULT_GAMBLE_TOLERANCE,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_BLOCK_HOURS,
    DEFAULT_RATE_START_KEY,
    DEFAULT_RATE_UNIT_MULTIPLIER,
    DEFAULT_RATE_VALUE_KEY,
    DEFAULT_RATES_ATTRIBUTE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)


def _required(key: str, defaults: dict[str, Any]) -> vol.Required:
    """vol.Required with a default only when one actually exists — an entity
    selector rejects a `None` default that voluptuous would otherwise insert for
    any key the user leaves blank."""
    if key in defaults:
        return vol.Required(key, default=defaults[key])
    return vol.Required(key)


def _optional(key: str, defaults: dict[str, Any]) -> vol.Optional:
    if key in defaults and defaults[key] is not None:
        return vol.Optional(key, default=defaults[key])
    return vol.Optional(key)


def _with_default(key: str, defaults: dict[str, Any], fallback: Any) -> vol.Required:
    return vol.Required(key, default=defaults.get(key, fallback))


def essential_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        _required(CONF_CURRENT_RATES_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="event")
        ),
        _required(CONF_NEXT_RATES_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="event")
        ),
        _optional(CONF_FORECAST_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        _required(CONF_CHARGER_STATE_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        _with_default(
            CONF_CHARGER_CONNECTED_STATES, defaults, DEFAULT_CHARGER_CONNECTED_STATES
        ): str,
    })


def advanced_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        _with_default(CONF_GAMBLE_TOLERANCE, defaults, DEFAULT_GAMBLE_TOLERANCE): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=100, step=5, mode=selector.NumberSelectorMode.SLIDER)
        ),
        _with_default(CONF_MIN_BLOCK_HOURS, defaults, DEFAULT_MIN_BLOCK_HOURS): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.5, max=4.0, step=0.5, mode=selector.NumberSelectorMode.SLIDER)
        ),
        _with_default(CONF_MAX_PRICE, defaults, DEFAULT_MAX_PRICE): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=200, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        _with_default(
            CONF_UPDATE_INTERVAL_MINUTES, defaults, DEFAULT_UPDATE_INTERVAL_MINUTES
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        _with_default(
            CONF_RATE_UNIT_MULTIPLIER, defaults, DEFAULT_RATE_UNIT_MULTIPLIER
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.01, max=1000, step=0.01, mode=selector.NumberSelectorMode.BOX)
        ),
        _with_default(CONF_RATES_ATTRIBUTE, defaults, DEFAULT_RATES_ATTRIBUTE): str,
        _with_default(CONF_RATE_START_KEY, defaults, DEFAULT_RATE_START_KEY): str,
        _with_default(CONF_RATE_VALUE_KEY, defaults, DEFAULT_RATE_VALUE_KEY): str,
        _with_default(CONF_FORECAST_ATTRIBUTE, defaults, DEFAULT_FORECAST_ATTRIBUTE): str,
        _with_default(CONF_FORECAST_DATETIME_KEY, defaults, DEFAULT_FORECAST_DATETIME_KEY): str,
        _with_default(CONF_FORECAST_PRICE_KEY, defaults, DEFAULT_FORECAST_PRICE_KEY): str,
    })


class WholesaleEvScheduleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle initial setup of the integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._essential: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._essential = user_input
            return await self.async_step_advanced()

        return self.async_show_form(step_id="user", data_schema=essential_schema({}))

    async def async_step_advanced(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title="Wholesale EV Schedule", data={}, options={**self._essential, **user_input}
            )

        return self.async_show_form(step_id="advanced", data_schema=advanced_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WholesaleEvScheduleOptionsFlow()


class WholesaleEvScheduleOptionsFlow(OptionsFlow):
    """Edit the entity wiring, tolerances, and parsing overrides after setup."""

    def __init__(self) -> None:
        self._essential: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._essential = user_input
            return await self.async_step_advanced()

        return self.async_show_form(
            step_id="init", data_schema=essential_schema(dict(self.config_entry.options))
        )

    async def async_step_advanced(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data={**self._essential, **user_input})

        return self.async_show_form(
            step_id="advanced", data_schema=advanced_schema(dict(self.config_entry.options))
        )
