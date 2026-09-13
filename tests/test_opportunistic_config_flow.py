"""Entity-creation gating for opportunistic charging (issue #53).

Opportunistic charging is entirely opt-in: CONF_ENABLE_OPPORTUNISTIC_CHARGING
defaults to False, and every opportunistic entity (switch.py's
opportunistic_charging_enabled, number.py's opportunistic_target_hours/
opportunistic_max_price, datetime.py's opportunistic_ready_by, sensor.py's
opportunistic_hours_remaining/opportunistic_next_slot_start/_end) is only
created when that option is explicitly True at setup time. This must hold
structurally, not by accident -- test_entity_naming.py's
test_every_entity_id_carries_the_default_instance_prefix already asserts an
EXACT set-equality of entity_ids against EXPECTED_ENTITY_IDS using the
default FULL_OPTIONS (opportunistic off), so that existing test is itself
the regression guard for "off produces nothing new"; this file additionally
proves the flip side (explicit off matches default-absent, and on produces
exactly the expected extra set) without touching that existing test.
"""

from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from custom_components.wholesale_ev_schedule.const import DOMAIN

from .factories import (
    EXPECTED_ENTITY_IDS,
    OPPORTUNISTIC_OPTIONS,
    async_setup_wholesale_entry,
    expected_opportunistic_entity_ids,
)


async def _registered_entity_ids(hass) -> set[str]:
    registry = er.async_get(hass)
    return {e.entity_id for e in registry.entities.values() if e.platform == DOMAIN}


async def test_opportunistic_disabled_produces_exactly_the_default_entity_set(hass):
    # Explicitly False must match the default-absent case FULL_OPTIONS
    # already represents -- no opportunistic entities, nothing missing from
    # the pre-existing default set either.
    await async_setup_wholesale_entry(hass)
    assert await _registered_entity_ids(hass) == EXPECTED_ENTITY_IDS


async def test_opportunistic_enabled_produces_the_default_set_plus_opportunistic_entities(hass):
    await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    all_ids = await _registered_entity_ids(hass)
    expected = EXPECTED_ENTITY_IDS | expected_opportunistic_entity_ids()
    assert all_ids == expected


async def test_opportunistic_entity_ids_carry_the_instance_prefix(hass):
    await async_setup_wholesale_entry(hass, options=OPPORTUNISTIC_OPTIONS)
    prefix = slugify("Wholesale EV Schedule")
    opportunistic_ids = await _registered_entity_ids(hass) - EXPECTED_ENTITY_IDS
    assert opportunistic_ids, "expected at least the opportunistic entities to be new"
    for entity_id in opportunistic_ids:
        _, object_id = entity_id.split(".", 1)
        assert object_id.startswith(f"{prefix}_")
