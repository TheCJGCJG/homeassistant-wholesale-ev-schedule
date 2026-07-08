"""Guards against entity_id collisions with an unrelated EV-charging setup running
on the same HA instance (e.g. the pyscript-based original this was ported from).
"""
from homeassistant.helpers import entity_registry as er

from custom_components.wholesale_ev_schedule.const import DOMAIN, ENTITY_ID_PREFIX

from .factories import EXPECTED_ENTITY_IDS, PYSCRIPT_ENTITY_IDS, async_setup_wholesale_entry


async def test_no_entity_id_collides_with_pyscript_original(hass):
    await async_setup_wholesale_entry(hass)

    registry = er.async_get(hass)
    all_ids = {entry.entity_id for entry in registry.entities.values()}

    collisions = all_ids & PYSCRIPT_ENTITY_IDS
    assert not collisions, f"entity_id collision with pyscript original: {collisions}"


async def test_every_entity_id_carries_the_integration_prefix(hass):
    await async_setup_wholesale_entry(hass)

    registry = er.async_get(hass)
    our_entities = [e for e in registry.entities.values() if e.platform == DOMAIN]
    assert our_entities, "expected entities to be registered"

    for entry in our_entities:
        _, object_id = entry.entity_id.split(".", 1)
        assert object_id.startswith(f"{ENTITY_ID_PREFIX}_"), (
            f"{entry.entity_id} does not carry the '{ENTITY_ID_PREFIX}' prefix"
        )

    assert {e.entity_id for e in our_entities} == EXPECTED_ENTITY_IDS
