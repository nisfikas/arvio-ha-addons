"""arvio.model builder — pure fixtures, no HA."""
import unittest
from unittest import mock

from helpers import (
    AREAS,
    AUTOMATION_CONFIGS,
    DEVICES,
    ENTITY_REGISTRY_DISPLAY,
    FLOORS,
    STATES,
    agent,
    build,
    display_to_list,
    st,
)


def by_id(model: dict) -> dict:
    return {e["entity_id"]: e for e in model["entities"]}


class ModelBuilderTest(unittest.TestCase):
    def test_floors_areas_devices(self):
        m = build()
        self.assertEqual(m["snapshot_version"], 41)
        self.assertEqual(m["ha_version"], "2025.8.1")
        self.assertEqual(m["timezone"], "Europe/Athens")
        self.assertEqual([f["floor_id"] for f in m["floors"]], ["isogeio", "orofos1"])
        self.assertEqual(m["floors"][0], {"floor_id": "isogeio", "name": "Ισόγειο", "level": 0, "icon": "mdi:home-floor-0", "aliases": []})
        self.assertEqual({a["area_id"] for a in m["areas"]}, {"saloni", "kouzina", "ypno", "apothiki"})
        saloni = next(a for a in m["areas"] if a["area_id"] == "saloni")
        self.assertEqual(saloni["floor_id"], "isogeio")
        self.assertEqual(saloni["icon"], "mdi:sofa")
        self.assertIn("temperature_entity_id", saloni)
        # §14.2: aliases pass through when the registry has them, else []
        self.assertEqual(saloni["aliases"], ["living"])
        self.assertEqual(next(a for a in m["areas"] if a["area_id"] == "kouzina")["aliases"], [])
        self.assertEqual(agent.normalize_floors([{"floor_id": "f", "name": "F", "aliases": ["ground"]}])[0]["aliases"], ["ground"])
        devs = {d["device_id"]: d for d in m["devices"]}
        self.assertEqual(devs["dev_kouzina"]["area_id"], "kouzina")
        self.assertEqual(devs["dev_orphan"]["name"], "Πρίζα")  # name_by_user wins
        self.assertEqual(m["sun"], {"next_rising": "2026-09-11T04:05:00+00:00", "next_setting": "2026-09-10T16:30:00+00:00"})

    def test_entity_area_and_floor_resolution(self):
        e = by_id(build())
        # entity area
        self.assertEqual(e["light.saloni"]["area_id"], "saloni")
        self.assertEqual(e["light.saloni"]["floor_id"], "isogeio")
        self.assertEqual(e["light.saloni"]["icon"], "mdi:ceiling-light")
        # entity without area, device with area
        self.assertEqual(e["light.kouzina"]["area_id"], "kouzina")
        self.assertEqual(e["light.kouzina"]["floor_id"], "isogeio")
        self.assertEqual(e["light.kouzina"]["device_id"], "dev_kouzina")
        # device without area → «Χωρίς δωμάτιο»
        self.assertIsNone(e["light.orphan"]["area_id"])
        self.assertIsNone(e["light.orphan"]["floor_id"])
        # area without floor → «Χωρίς όροφο»
        self.assertEqual(e["switch.apothiki"]["area_id"], "apothiki")
        self.assertIsNone(e["switch.apothiki"]["floor_id"])
        # no registry entry at all
        self.assertIsNone(e["alarm_control_panel.spiti"]["area_id"])

    def test_filters(self):
        e = by_id(build())
        self.assertNotIn("switch.config_thing", e)  # entity_category config
        self.assertNotIn("sensor.diag_thing", e)  # entity_category diagnostic
        self.assertIn("script.arvio_kalinyxta", e)  # prefix script.arvio_ (§14.2)
        self.assertNotIn("script.other", e)  # label "arvio" alone is not enough
        self.assertNotIn("light.hidden_one", e)  # list_for_display hb (hidden)
        self.assertIn("binary_sensor.porta", e)  # door
        self.assertNotIn("binary_sensor.kinisi", e)  # motion
        self.assertIn("sensor.thermo", e)  # temperature
        self.assertNotIn("sensor.power", e)
        # 0.1.21: media_player is exposed (device_class speaker|tv|receiver|null)
        self.assertIn("media_player.tv", e)  # no device_class, no registry entry
        self.assertIn("media_player.saloni", e)
        self.assertNotIn("media_player.projector", e)  # device_class outside the list
        self.assertNotIn("sun.sun", e)
        self.assertNotIn("automation.fevgo", e)
        for item in e.values():
            self.assertIsNone(item["entity_category"])
            self.assertIn(item["domain"], agent.HOME_ENTITY_DOMAINS)

    def test_doorbell_call_reaches_model_as_diagnostic(self):
        eid = "binary_sensor.vto_button_pressed"
        call = st(eid, "off", friendly_name="Κλήση")
        cam = st("camera.vto_main", "idle", friendly_name="Κουδούνι")
        raw = {
            **ENTITY_REGISTRY_DISPLAY,
            "entities": [
                *ENTITY_REGISTRY_DISPLAY["entities"],
                {"ei": "camera.vto_main", "pl": "onvif"},
                {"ei": eid, "pl": "dahua", "ec": 1, "hb": True},
            ],
        }
        self.assertEqual(
            agent.doorbell_call_ids_from_cameras(["camera.vto_main", "light.x"]),
            {
                "binary_sensor.vto_button_pressed",
                "binary_sensor.vto_call",
                "binary_sensor.vto_doorbell",
            },
        )
        self.assertNotIn(eid, by_id(build(states=STATES + [call], entity_registry_raw=raw)))
        row = by_id(build(states=STATES + [cam, call], entity_registry_raw=raw))[eid]
        self.assertEqual(row["entity_category"], "diagnostic")
        self.assertEqual(row["domain"], "binary_sensor")
        self.assertEqual(row["name"], "Κλήση")

    def test_typed_attrs(self):
        e = by_id(build())
        light = e["light.saloni"]["attrs"]
        self.assertEqual(light["brightness_pct"], 50)
        self.assertEqual(light["color_temp_kelvin"], 2700)
        self.assertEqual(light["min_color_temp_kelvin"], 2000)
        self.assertEqual(light["max_color_temp_kelvin"], 6500)
        self.assertTrue(e["light.saloni"]["capabilities"]["color_temp"])
        self.assertTrue(e["light.saloni"]["capabilities"]["brightness"])
        # mireds-only HA (< 2022.12): kelvin derived
        old = e["light.orphan"]["attrs"]
        self.assertEqual(old["color_temp_kelvin"], 4000)
        self.assertEqual(old["min_color_temp_kelvin"], 2000)
        self.assertEqual(old["max_color_temp_kelvin"], 6536)
        self.assertEqual(old["brightness_pct"], 100)
        self.assertEqual(e["light.kouzina"]["attrs"]["brightness_pct"], None)
        climate = e["climate.ypno"]["attrs"]
        self.assertEqual(climate["hvac_mode"], "heat")
        self.assertEqual(climate["hvac_action"], "heating")
        self.assertEqual(climate["current_temperature"], 21.5)
        self.assertEqual(climate["hvac_modes"], ["off", "heat"])
        self.assertTrue(e["climate.ypno"]["capabilities"]["fan"])
        self.assertEqual(e["cover.ypno"]["attrs"]["current_position"], 60)
        self.assertTrue(e["cover.ypno"]["capabilities"]["position"])
        group = agent.typed_attrs("light", "on", {"entity_id": ["light.a", "light.b"], "brightness": 10})
        self.assertEqual(group["light_entity_ids"], ["light.a", "light.b"])
        self.assertNotIn("light_entity_ids", e["light.saloni"]["attrs"])
        lock = e["lock.eisodos"]["attrs"]
        self.assertNotIn("is_jammed", lock)  # §14.2: "jammed" is a state
        self.assertEqual(e["lock.eisodos"]["state"], "jammed")
        self.assertEqual(lock["changed_by"], "Nick")
        alarm = e["alarm_control_panel.spiti"]["attrs"]
        self.assertIs(alarm["code_arm_required"], False)
        self.assertEqual(alarm["code_format"], "number")
        self.assertEqual(alarm["arming_time"], 30)
        self.assertEqual(alarm["delay_time"], 15)
        self.assertEqual(e["alarm_control_panel.spiti"]["supported_features"], 7)
        # §14.2: arming_time / delay_time only when the panel reports them
        bare = agent.typed_attrs("alarm_control_panel", "disarmed", {"code_arm_required": True})
        self.assertNotIn("arming_time", bare)
        self.assertNotIn("delay_time", bare)
        self.assertEqual(e["scene.vradi"]["attrs"]["scene_entity_ids"], ["light.saloni", "cover.ypno"])
        self.assertEqual(e["script.arvio_kalinyxta"]["attrs"]["labels"], ["arvio"])
        self.assertEqual(e["sensor.thermo"]["attrs"], {"value": 21.5, "unit_of_measurement": "°C"})
        self.assertEqual(e["sensor.thermo"]["device_class"], "temperature")
        self.assertEqual(e["binary_sensor.porta"]["attrs"]["value"], "on")

    def test_without_floors_registry(self):
        # HA < 2024.4: no floor registry → floors [] and every floor_id null.
        m = build(floors_raw=None, ha_version="2023.12.4")
        self.assertEqual(m["floors"], [])
        self.assertTrue(all(e["floor_id"] is None for e in m["entities"]))
        self.assertTrue(all(a["floor_id"] is None for a in m["areas"]))
        self.assertEqual(by_id(m)["light.saloni"]["area_id"], "saloni")

    def test_full_registry_list_shape(self):
        m_display = build()
        m_list = build(entity_registry_raw=display_to_list(ENTITY_REGISTRY_DISPLAY))
        self.assertEqual(
            [(e["entity_id"], e["area_id"], e["floor_id"]) for e in m_display["entities"]],
            [(e["entity_id"], e["area_id"], e["floor_id"]) for e in m_list["entities"]],
        )
        self.assertEqual(m_display["total"], m_list["total"])

    def test_paging(self):
        full = build()
        self.assertEqual(full["offset"], 0)
        self.assertEqual(full["limit"], agent.MODEL_LIMIT_DEFAULT)
        self.assertGreater(full["total"], 5)
        self.assertEqual(len(full["entities"]), full["total"])  # under the default limit
        page = build(offset=2, limit=3)
        self.assertEqual(page["offset"], 2)
        self.assertEqual(page["limit"], 3)
        self.assertEqual(page["total"], full["total"])
        self.assertEqual(
            [e["entity_id"] for e in page["entities"]],
            [e["entity_id"] for e in full["entities"][2:5]],
        )
        tail = build(offset=full["total"] - 1, limit=50)
        self.assertEqual(len(tail["entities"]), 1)
        self.assertEqual(build(limit=9999)["limit"], agent.MODEL_LIMIT_MAX)
        self.assertEqual(build(limit="junk", offset=-4)["limit"], agent.MODEL_LIMIT_DEFAULT)
        self.assertEqual(build(offset=-4)["offset"], 0)
        # floors/areas/scenarios are never paged
        self.assertEqual(len(page["floors"]), 2)
        self.assertEqual(len(page["scenarios"]), 2)

    def test_entities_stable_order(self):
        ids = [e["entity_id"] for e in build()["entities"]]
        self.assertEqual(ids, sorted(ids))

    def test_scenarios(self):
        m = build()
        s = {x["scenario_id"]: x for x in m["scenarios"]}
        self.assertEqual(set(s), {"arvio_fevgo", "arvio_nyxta"})
        self.assertTrue(s["arvio_fevgo"]["show_in_home"])
        self.assertEqual(s["arvio_fevgo"]["name"], "Φεύγω")
        self.assertEqual(
            s["arvio_fevgo"]["action_entity_ids"],
            ["light.kouzina", "light.saloni", "lock.eisodos", "switch.apothiki"],
        )
        self.assertFalse(s["arvio_nyxta"]["show_in_home"])
        self.assertEqual(s["arvio_nyxta"]["action_entity_ids"], [])
        self.assertFalse(s["arvio_nyxta"]["enabled"])

    def test_scenarios_without_configs(self):
        m = build(automation_configs=None)
        s = {x["scenario_id"]: x for x in m["scenarios"]}
        self.assertFalse(s["arvio_fevgo"]["show_in_home"])
        self.assertEqual(s["arvio_fevgo"]["name"], "Φεύγω")  # friendly_name fallback

    def test_empty_inputs(self):
        m = agent.build_hub_model([], None, None, None, None, None, snapshot_version=1)
        self.assertEqual(m["entities"], [])
        self.assertEqual(m["total"], 0)
        self.assertEqual(m["floors"], [])
        self.assertIsNone(m["sun"])


class ShowInHomeTest(unittest.TestCase):
    def test_variants(self):
        self.assertTrue(agent.parse_show_in_home('{"arvio": {"show_in_home": true}}'))
        self.assertTrue(agent.parse_show_in_home('Text before {"arvio":{"show_in_home":true}} after'))
        self.assertFalse(agent.parse_show_in_home('{"arvio": {"show_in_home": false}}'))
        self.assertFalse(agent.parse_show_in_home('{"arvio": {}}'))
        self.assertFalse(agent.parse_show_in_home("no json here"))
        self.assertTrue(agent.parse_show_in_home("Managed by Arvio Partner"))
        self.assertFalse(agent.parse_show_in_home(None))
        self.assertFalse(agent.parse_show_in_home("{broken"))
        self.assertTrue(agent.scenario_show_in_home({"variables": {"arvio_show_in_home": True}}))
        self.assertFalse(agent.scenario_show_in_home({"description": "x", "variables": {}}))


class RegistryFingerprintTest(unittest.TestCase):
    def test_changes_on_area_move(self):
        base = agent.registry_fingerprint(
            agent.normalize_floors(FLOORS), agent.normalize_areas(AREAS), agent.normalize_devices(DEVICES),
            agent.normalize_entity_registry(ENTITY_REGISTRY_DISPLAY),
        )
        moved = {**ENTITY_REGISTRY_DISPLAY, "entities": [
            dict(e, ai="kouzina") if e["ei"] == "light.saloni" else e for e in ENTITY_REGISTRY_DISPLAY["entities"]
        ]}
        other = agent.registry_fingerprint(
            agent.normalize_floors(FLOORS), agent.normalize_areas(AREAS), agent.normalize_devices(DEVICES),
            agent.normalize_entity_registry(moved),
        )
        self.assertNotEqual(base, other)
        self.assertEqual(len(base), 16)

    def test_snapshot_version_persists_and_bumps(self):
        agent.save_hub({})
        v1, bumped = agent.ensure_snapshot_for_fingerprint("aaaa")
        self.assertEqual((v1, bumped), (1, False))  # first fingerprint: no bump
        v2, bumped = agent.ensure_snapshot_for_fingerprint("aaaa")
        self.assertEqual((v2, bumped), (1, False))
        v3, bumped = agent.ensure_snapshot_for_fingerprint("bbbb")
        self.assertEqual((v3, bumped), (2, True))
        self.assertEqual(agent.get_snapshot_version(), 2)
        self.assertEqual(agent.bump_snapshot_version("test"), 3)
        self.assertEqual(agent.load_hub()["snapshot_version"], 3)
        self.assertEqual(agent.load_hub()["registry_fingerprint"], "bbbb")
        # §14.3: a registry-updated event bumps even when the fingerprint did not change
        v4, bumped = agent.ensure_snapshot_for_fingerprint("bbbb", force=True)
        self.assertEqual((v4, bumped), (4, True))

    def test_registry_event_forces_bump_and_push(self):
        agent.save_hub({"snapshot_version": 5, "registry_fingerprint": "x"})
        raw = {"floors": FLOORS, "areas": AREAS, "devices": DEVICES, "entity_registry": ENTITY_REGISTRY_DISPLAY}
        with mock.patch.object(agent, "fetch_registries", return_value=raw), \
             mock.patch.object(agent, "push_model_changed") as pushed:
            self.assertTrue(agent.refresh_registry_cache("model"))  # first load: fingerprint stored, no bump
            pushed.assert_called_once()  # fingerprint "x" → real one counts as a change
            pushed.reset_mock()
            self.assertTrue(agent.refresh_registry_cache("model"))  # unchanged → silent
            pushed.assert_not_called()
            self.assertTrue(agent.refresh_registry_cache("area_registry_updated", force_bump=True))
            pushed.assert_called_once_with(7)
        self.assertEqual(agent.get_snapshot_version(), 7)


class WeatherBlockTest(unittest.TestCase):
    def test_no_weather_entity_is_none(self):
        m = build()
        self.assertIsNone(m["weather"])
        self.assertNotIn("weather.home", by_id(m))

    def test_forecasts_plural_envelope(self):
        states = STATES + [
            st("weather.home", "sunny", temperature=22.5, humidity=48),
        ]
        forecasts = {
            "weather.home": {
                "forecast": [
                    {"datetime": "2026-09-16T00:00:00+00:00", "condition": "sunny", "temperature": 28, "templow": 18, "precipitation": 0},
                    {"datetime": "2026-09-17T00:00:00+00:00", "condition": "rainy", "temperature": 21, "templow": 14},
                ]
            }
        }
        w = agent.weather_block(states, forecasts)
        self.assertEqual(w["entity_id"], "weather.home")
        self.assertEqual(w["condition"], "sunny")
        self.assertEqual(w["temperature"], 22.5)
        self.assertEqual(w["humidity"], 48)
        self.assertEqual(len(w["forecast"]), 2)
        self.assertEqual(w["forecast"][0]["templow"], 18)
        self.assertEqual(w["hourly"], [])
        m = build(states=states, weather=w)
        self.assertEqual(m["weather"]["entity_id"], "weather.home")
        self.assertNotIn("weather.home", by_id(m))

    def test_missing_forecasts_are_empty_not_stale(self):
        states = STATES + [st("weather.home", "cloudy", temperature=19)]
        w = agent.weather_block(states, None)
        self.assertEqual(w["forecast"], [])
        self.assertEqual(w["hourly"], [])
        self.assertEqual(w["temperature"], 19)

    def test_hourly_forecasts_are_capped_at_24(self):
        states = STATES + [st("weather.home", "sunny", temperature=22)]
        hourly = {
            "weather.home": {
                "forecast": [
                    {"datetime": f"2026-09-16T{h:02d}:00:00+00:00", "condition": "sunny", "temperature": 20}
                    for h in range(30)
                ]
            }
        }
        w = agent.weather_block(states, None, hourly)
        self.assertEqual(w["forecast"], [])
        self.assertEqual(len(w["hourly"]), 24)
        self.assertEqual(w["hourly"][0]["datetime"], "2026-09-16T00:00:00+00:00")


class VersionHelpersTest(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(agent.parse_ha_version("2025.8.1"), (2025, 8))
        self.assertEqual(agent.parse_ha_version("2024.4"), (2024, 4))
        self.assertIsNone(agent.parse_ha_version("dev"))
        self.assertTrue(agent.ha_version_at_least("2024.4.0", (2024, 4)))
        self.assertFalse(agent.ha_version_at_least("2024.3.9", (2024, 4)))
        self.assertTrue(agent.ha_version_at_least(None, (2024, 4)))  # unknown → assume current
        self.assertEqual(agent.mireds_to_kelvin(250), 4000)
        self.assertEqual(agent.kelvin_to_mireds(2700), 370)
        self.assertIsNone(agent.mireds_to_kelvin(0))

    def test_parse_iso(self):
        self.assertEqual(agent.parse_iso_ts("1970-01-01T00:00:10Z"), 10.0)
        self.assertEqual(agent.parse_iso_ts("1970-01-01T00:00:10.500Z"), 10.5)
        self.assertEqual(agent.parse_iso_ts("1970-01-01T02:00:10+02:00"), 10.0)
        self.assertIsNone(agent.parse_iso_ts("soon"))
        self.assertIsNone(agent.parse_iso_ts(None))
        self.assertTrue(agent.now_iso().endswith("Z"))


if __name__ == "__main__":
    unittest.main()
