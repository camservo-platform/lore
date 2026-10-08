import pytest

from lore import catalog, worldgen
from lore.catalog import CatalogError


def test_core_catalog_is_valid_and_unique():
    catalog.check_unique(catalog.core())
    for option in catalog.core():
        assert catalog.validate(option) == option  # nothing clamped or dropped
    assert {o["kind"] for o in catalog.core()} == {"race", "class"}


def test_abilities_for_follow_the_class_resource():
    arcanist = catalog.find(catalog.core(), "class", "ARCANIST")
    level1 = catalog.abilities_for(arcanist, 1)
    assert all(a["cost"] is not None and a["max_uses"] is None for a in level1)
    assert "Starfall" not in [a["name"] for a in level1]
    assert catalog.pool_max(arcanist, 3) == 8
    shade = catalog.find(catalog.core(), "class", "shade")
    vanish = next(a for a in catalog.abilities_for(shade, 1) if a["name"] == "Vanish")
    assert (vanish["max_uses"], vanish["refresh"], vanish["source"]) == (2, "session", "class: Shade")


def test_validate_refuses_or_clamps_bad_options():
    with pytest.raises(CatalogError, match="kind"):
        catalog.validate({"kind": "monster"})
    with pytest.raises(CatalogError, match="level 1"):
        catalog.validate({"kind": "race", "name": "X", "summary": "s", "description": "d", "abilities": [
            {"name": "A", "summary": "s", "description": "d", "effect": "e", "level": 3}]})
    big = catalog.validate({"kind": "class", "name": "Titan", "summary": "s", "description": "d", "hp": 99,
                            "resource": {"kind": "pool", "name": "Might", "base": 50, "per_level": 9},
                            "abilities": [{"name": "Smash", "summary": "s", "description": "d", "effect": "e",
                                           "cost": 40}]})
    assert (big["hp"], big["resource"]["base"], big["abilities"][0]["cost"]) == (16, 10, 6)


def test_forged_extras_become_valid_options():
    extra = {"kind": "class", "name": "Gunner", "summary": "s", "description": "d", "hp": 10, "defense": 13,
             "uses_pool": True, "pool_name": "Ammo", "refresh": "session",
             "abilities": [{"name": "Burst", "summary": "s", "description": "d", "effect": "2d6 damage",
                            "level": 1, "limit": 2}]}
    option = catalog.validate(worldgen.extra_option(extra))
    assert option["resource"] == {"kind": "pool", "name": "Ammo", "base": 3, "per_level": 1, "refresh": "session"}
    assert option["abilities"][0]["cost"] == 2
    assert "class arcanist: Arcanist [pool: Aether, refresh: recovery]" in worldgen.core_summary()
