"""Dependency-light tests for dashboard and Apple TV source extraction."""

import asyncio
from types import SimpleNamespace

from simulate_adapter_acceptance import const, sources_module

SourceKind = const.SourceKind
_select_dashboard_instance = sources_module._select_dashboard_instance
async_read_source = sources_module.async_read_source
extract_dashboard_entities = sources_module.extract_dashboard_entities


def test_extract_selected_view_and_nested_entities() -> None:
    result = extract_dashboard_entities(
        {
            "views": [
                {"path": "other", "cards": [{"entity": "light.skip"}]},
                {
                    "path": "default-view",
                    "sections": [
                        {
                            "type": "heading",
                            "heading": "客廳",
                            "cards": [
                                {"entity": "light.main"},
                                {"entities": ["switch.fan", {"entity": "sensor.temp"}]},
                            ],
                        }
                    ],
                },
            ]
        },
        "default-view",
    )
    assert result.entities == {"light.main", "switch.fan", "sensor.temp"}
    assert "light.skip" not in result.entities
    assert result.rooms["switch.fan"] == "客廳"


def test_extract_camera_specific_fields_and_arbitrary_nested_cards() -> None:
    result = extract_dashboard_entities(
        {
            "views": [
                {
                    "path": "default-view",
                    "cards": [
                        {
                            "type": "picture-elements",
                            "camera_image": "camera.front_door",
                            "elements": [
                                {"type": "state-icon", "entity": "camera.side"}
                            ],
                        },
                        {
                            "type": "custom:camera-card",
                            "camera_entity": "camera.live_view",
                            "card": {"entity": "sensor.camera_health"},
                            "visibility": [
                                {
                                    "condition": "state",
                                    "entity": "input_boolean.visibility_only",
                                }
                            ],
                        },
                        {
                            "type": "conditional",
                            "conditions": [
                                {
                                    "condition": "state",
                                    "entity": "binary_sensor.condition_only",
                                }
                            ],
                            "card": {"entity": "light.visible_card"},
                        },
                    ],
                }
            ]
        },
        "default-view",
    )
    assert result.entities == {
        "camera.front_door",
        "camera.side",
        "camera.live_view",
        "sensor.camera_health",
        "light.visible_card",
    }


def test_named_lovelace_dashboard_precedes_legacy_default() -> None:
    named = object()
    legacy = object()
    assert (
        _select_dashboard_instance({None: legacy, "lovelace": named}, "lovelace")
        is named
    )


def test_legacy_default_remains_supported() -> None:
    legacy = object()
    assert _select_dashboard_instance({None: legacy}, "lovelace") is legacy


def test_view_title_is_used_when_path_is_null_or_empty() -> None:
    for path_value, entity_id in (
        (None, "light.null_path"),
        ("", "light.empty_path"),
    ):
        result = extract_dashboard_entities(
            {
                "views": [
                    {
                        "title": "Title fallback",
                        "path": path_value,
                        "cards": [{"entity": entity_id}],
                    }
                ]
            },
            "Title fallback",
        )
        assert result.entities == {entity_id}


class _Dashboard:
    def __init__(self, config: dict) -> None:
        self._config = config

    async def async_load(self, _force: bool) -> dict:
        return self._config


def test_multiple_dashboard_views_union_and_drop_conflicting_room() -> None:
    dashboard = _Dashboard(
        {
            "views": [
                {
                    "path": "one",
                    "sections": [
                        {
                            "title": "客廳",
                            "cards": [
                                {"entity": "light.shared"},
                                {"entity": "switch.one"},
                            ],
                        }
                    ],
                },
                {
                    "path": "two",
                    "sections": [
                        {
                            "title": "餐廳",
                            "cards": [
                                {"entity": "light.shared"},
                                {"entity": "sensor.two"},
                            ],
                        }
                    ],
                },
            ]
        }
    )
    hass = SimpleNamespace(
        data={"lovelace": SimpleNamespace(dashboards={"lovelace": dashboard})}
    )
    config = SimpleNamespace(
        source_kind=SourceKind.DASHBOARD,
        source_dashboard="lovelace",
        source_view="one",
        source_pages=(("lovelace", "one"), ("lovelace", "two")),
    )

    result = asyncio.run(async_read_source(hass, config))

    assert result.entities == {"light.shared", "switch.one", "sensor.two"}
    assert "light.shared" not in result.rooms
    assert result.rooms == {"switch.one": "客廳", "sensor.two": "餐廳"}


def test_multiple_dashboard_views_empty_aggregate_fails_closed() -> None:
    dashboard = _Dashboard({"views": [{"path": "empty", "cards": []}]})
    hass = SimpleNamespace(
        data={"lovelace": SimpleNamespace(dashboards={"lovelace": dashboard})}
    )
    config = SimpleNamespace(
        source_kind=SourceKind.DASHBOARD,
        source_dashboard="lovelace",
        source_view="empty",
        source_pages=(("lovelace", "empty"),),
    )

    try:
        asyncio.run(async_read_source(hass, config))
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("empty multi-view source must fail closed")


def test_apple_tv_provenance_uses_supported_device_lookup() -> None:
    class Device:
        def __init__(self, device_id: str, entry_id: str) -> None:
            self.id = device_id
            self.config_entry_id = entry_id

        @property
        def config_entries(self):
            raise AssertionError("Deprecated device config_entries accessed")

    class Registry:
        @property
        def devices(self):
            raise AssertionError("Device registry mapping accessed")

    device_registry = Registry()
    devices = (
        Device("apple-one", "entry-one"),
        Device("apple-two", "entry-two"),
        Device("unrelated", "other-entry"),
    )
    entities = {
        entity_id: SimpleNamespace(
            entity_id=entity_id, config_entry_id=entry_id, device_id=device_id
        )
        for entity_id, entry_id, device_id in (
            ("media_player.direct", "entry-one", None),
            ("remote.device_one", None, "apple-one"),
            ("remote.device_two", "other-entry", "apple-two"),
            ("media_player.unrelated", "other-entry", "unrelated"),
            ("sensor.unassigned", None, None),
            ("sensor.missing_device", None, "missing"),
        )
    }
    original_get = sources_module.dr.async_get
    original_lookup = sources_module.dr.async_entries_for_config_entry
    original_entity_get = sources_module.er.async_get
    lookups = []

    def lookup(registry, entry_id):
        assert registry is device_registry
        lookups.append(entry_id)
        return [device for device in devices if device.config_entry_id == entry_id]

    try:
        sources_module.dr.async_get = lambda _hass: device_registry
        sources_module.dr.async_entries_for_config_entry = lookup
        sources_module.er.async_get = lambda _hass: SimpleNamespace(entities=entities)
        for entry_ids, expected in (
            (("entry-one", "entry-two"), {
                "media_player.direct", "remote.device_one", "remote.device_two"
            }),
            (("entry-one", "entry-without-devices"), {
                "media_player.direct", "remote.device_one"
            }),
            ((), set()),
        ):
            lookups.clear()

            def entries(domain):
                assert domain == "apple_tv"
                return [SimpleNamespace(entry_id=value) for value in entry_ids]

            hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=entries))
            result = asyncio.run(sources_module.async_find_apple_tv_entities(hass))
            assert result == expected
            assert set(lookups) == set(entry_ids)
            assert len(lookups) == len(entry_ids)
    finally:
        sources_module.dr.async_get = original_get
        sources_module.dr.async_entries_for_config_entry = original_lookup
        sources_module.er.async_get = original_entity_get


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().copy().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"PASS: {len(tests)} source tests")
