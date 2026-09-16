"""Test bootstrap: import agent.py without a hub (scratch /data, no HA)."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("ARVIO_DATA_DIR", tempfile.mkdtemp(prefix="arvio-agent-test-"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent  # noqa: E402

FLOORS = [
    {"floor_id": "isogeio", "name": "Ισόγειο", "level": 0, "icon": "mdi:home-floor-0", "aliases": []},
    {"floor_id": "orofos1", "name": "1ος", "level": 1, "icon": None, "aliases": []},
]

AREAS = [
    {"area_id": "saloni", "name": "Σαλόνι", "floor_id": "isogeio", "icon": "mdi:sofa", "picture": None, "aliases": ["living"]},
    {"area_id": "kouzina", "name": "Κουζίνα", "floor_id": "isogeio", "icon": None, "picture": None},
    {"area_id": "ypno", "name": "Υπνοδωμάτιο", "floor_id": "orofos1", "icon": None, "picture": None},
    {"area_id": "apothiki", "name": "Αποθήκη", "floor_id": None, "icon": None, "picture": None},
]

DEVICES = [
    {"id": "dev_kouzina", "name": "Kitchen dimmer", "name_by_user": None, "area_id": "kouzina"},
    {"id": "dev_orphan", "name": "Plug", "name_by_user": "Πρίζα", "area_id": None},
]

# config/entity_registry/list_for_display shape
ENTITY_REGISTRY_DISPLAY = {
    "entity_categories": {"0": "config", "1": "diagnostic"},
    "entities": [
        {"ei": "light.saloni", "pl": "hue", "ai": "saloni", "ic": "mdi:ceiling-light"},
        {"ei": "light.kouzina", "pl": "zha", "di": "dev_kouzina"},
        {"ei": "light.orphan", "pl": "zha", "di": "dev_orphan"},
        {"ei": "switch.config_thing", "pl": "zha", "ai": "saloni", "ec": 0},
        {"ei": "sensor.diag_thing", "pl": "zha", "ai": "saloni", "ec": 1},
        {"ei": "script.arvio_kalinyxta", "pl": "script", "lb": ["arvio"]},
        # label "arvio" but no `script.arvio_` prefix → not exposed (§14.2)
        {"ei": "script.other", "pl": "script", "lb": ["arvio"]},
        {"ei": "cover.ypno", "pl": "zha", "ai": "ypno"},
        {"ei": "binary_sensor.porta", "pl": "zha", "ai": "saloni"},
        {"ei": "binary_sensor.kinisi", "pl": "zha", "ai": "saloni"},
        {"ei": "sensor.thermo", "pl": "zha", "ai": "saloni"},
        {"ei": "sensor.power", "pl": "zha", "ai": "saloni"},
        {"ei": "lock.eisodos", "pl": "zha", "ai": "saloni"},
        {"ei": "alarm_control_panel.spiti", "pl": "manual"},
        {"ei": "switch.apothiki", "pl": "zha", "ai": "apothiki"},
        {"ei": "light.hidden_one", "pl": "zha", "ai": "kouzina", "hb": True},
        {"ei": "climate.ypno", "pl": "zha", "ai": "ypno"},
        {"ei": "scene.vradi", "pl": "homeassistant"},
        # 0.1.21 music: Music Assistant speaker (leader of a sync group) + a Cast speaker
        {"ei": "media_player.saloni", "pl": "music_assistant", "ai": "saloni"},
        {"ei": "media_player.kouzina", "pl": "cast", "ai": "kouzina"},
        {"ei": "media_player.projector", "pl": "cast", "ai": "saloni"},
        {"ei": "camera.front", "pl": "onvif", "ai": "saloni"},
    ],
}

# MediaPlayerEntityFeature bits used by the fixtures
MA_SPEAKER_FEATURES = (
    1 | 4 | 8 | 16 | 32 | 512 | 2048 | 4096 | 16384 | 32768 | 131072 | 262144 | 524288 | 2097152 | 4194304
)
CAST_SPEAKER_FEATURES = 1 | 4 | 8 | 512 | 4096 | 16384 | 131072 | 524288
SPEAKER_PICTURE = "/api/media_player_proxy/media_player.saloni?token=tok123&cache=abc"


def display_to_list(display: dict) -> list:
    """Same registry in config/entity_registry/list shape."""
    cats = display["entity_categories"]
    out = []
    for e in display["entities"]:
        out.append(
            {
                "entity_id": e["ei"],
                "device_id": e.get("di"),
                "area_id": e.get("ai"),
                "entity_category": cats.get(str(e["ec"])) if "ec" in e else None,
                "icon": e.get("ic"),
                "labels": e.get("lb", []),
                "hidden_by": "user" if e.get("hb") else None,
                "disabled_by": None,
            }
        )
    return out


def st(eid: str, state: str, **attrs) -> dict:
    return {
        "entity_id": eid,
        "state": state,
        "attributes": attrs,
        "last_changed": "2026-09-10T10:00:00.000000+00:00",
        "context": {"id": "ctx_" + eid.replace(".", "_"), "parent_id": None, "user_id": None},
    }


STATES = [
    st("light.saloni", "on", friendly_name="Φως σαλονιού", brightness=128, color_temp_kelvin=2700,
       min_color_temp_kelvin=2000, max_color_temp_kelvin=6500, supported_color_modes=["color_temp"]),
    st("light.kouzina", "off", friendly_name="Φως κουζίνας", supported_color_modes=["brightness"]),
    st("light.orphan", "on", friendly_name="Ορφανό", brightness=255, color_temp=250, min_mireds=153, max_mireds=500,
       supported_color_modes=["color_temp"]),
    st("switch.config_thing", "on", friendly_name="Config switch"),
    st("sensor.diag_thing", "12", friendly_name="Diag", device_class="temperature"),
    st("script.arvio_kalinyxta", "off", friendly_name="Καληνύχτα"),
    st("script.other", "off", friendly_name="Other"),
    st("cover.ypno", "open", friendly_name="Ρολό", current_position=60),
    st("binary_sensor.porta", "on", friendly_name="Πόρτα", device_class="door"),
    st("binary_sensor.kinisi", "off", friendly_name="Κίνηση", device_class="motion"),
    st("sensor.thermo", "21.5", friendly_name="Θερμοκρασία", device_class="temperature", unit_of_measurement="°C"),
    st("sensor.power", "120", friendly_name="Ισχύς", device_class="power", unit_of_measurement="W"),
    st("lock.eisodos", "jammed", friendly_name="Είσοδος", changed_by="Nick"),
    st("alarm_control_panel.spiti", "disarmed", friendly_name="Συναγερμός", code_arm_required=False, code_format="number",
       arming_time=30, delay_time=15, supported_features=7),
    st("switch.apothiki", "off", friendly_name="Αποθήκη"),
    st("light.hidden_one", "off", friendly_name="Κρυφό"),
    st("climate.ypno", "heat", friendly_name="Κλίμα", current_temperature=21.5, temperature=22, hvac_modes=["off", "heat"],
       hvac_action="heating", min_temp=7, max_temp=30, fan_modes=["auto"]),
    st("scene.vradi", "unknown", friendly_name="Βράδυ", entity_id=["light.saloni", "cover.ypno"]),
    st("media_player.tv", "off", friendly_name="TV"),
    st("media_player.saloni", "playing", friendly_name="Ηχείο σαλονιού", device_class="speaker",
       media_title="Blue in Green", media_artist="Miles Davis", media_album_name="Kind of Blue", app_name="Spotify",
       media_content_id="spotify://track/abc", media_content_type="music", media_duration=337, media_position=42.5,
       media_position_updated_at="2026-09-11T10:00:00+00:00", volume_level=0.35, is_volume_muted=False,
       source="Spotify", source_list=["Spotify", "Radio"], group_members=["media_player.saloni", "media_player.kouzina"],
       shuffle=False, repeat="off", supported_features=MA_SPEAKER_FEATURES, entity_picture=SPEAKER_PICTURE),
    st("media_player.kouzina", "paused", friendly_name="Κουζίνα Cast", device_class="speaker", volume_level=0.2,
       supported_features=CAST_SPEAKER_FEATURES),
    # device_class outside speaker|tv|receiver|null → not exposed (§15.2)
    st("media_player.projector", "on", friendly_name="Projector", device_class="projector"),
    st("camera.front", "idle", friendly_name="Είσοδος",
       supported_features=2, entity_picture="/api/camera_proxy/camera.front?token=SECRET"),
    st("sun.sun", "above_horizon", next_rising="2026-09-11T04:05:00+00:00", next_setting="2026-09-10T16:30:00+00:00"),
    st("automation.fevgo", "on", friendly_name="Φεύγω", id="arvio_fevgo"),
    st("automation.nyxta", "off", friendly_name="Νύχτα", id="arvio_nyxta"),
    st("automation.other", "on", friendly_name="Other", id="1699999"),
]

AUTOMATION_CONFIGS = {
    "arvio_fevgo": {
        "id": "arvio_fevgo",
        "alias": "Φεύγω",
        "description": 'Σκηνή εξόδου. {"arvio": {"show_in_home": true}}',
        "action": [
            {"service": "light.turn_off", "target": {"entity_id": ["light.saloni", "light.kouzina"]}},
            {"action": "lock.lock", "target": {"entity_id": "lock.eisodos"}},
            {"choose": [{"conditions": [], "sequence": [{"service": "switch.turn_off", "data": {"entity_id": "switch.apothiki"}}]}]},
        ],
    },
    "arvio_nyxta": {"id": "arvio_nyxta", "alias": "Νύχτα", "description": "plain text, no flag", "action": []},
}


def build(**kw):
    args = dict(
        states=STATES,
        floors_raw=FLOORS,
        areas_raw=AREAS,
        devices_raw=DEVICES,
        entity_registry_raw=ENTITY_REGISTRY_DISPLAY,
        automation_configs=AUTOMATION_CONFIGS,
        ha_version="2025.8.1",
        timezone_name="Europe/Athens",
        snapshot_version=41,
    )
    args.update(kw)
    return agent.build_hub_model(
        args.pop("states"),
        args.pop("floors_raw"),
        args.pop("areas_raw"),
        args.pop("devices_raw"),
        args.pop("entity_registry_raw"),
        args.pop("automation_configs"),
        **args,
    )


def regs() -> dict:
    """Normalised registries like agent.registry_snapshot()."""
    return {
        "floors": agent.normalize_floors(FLOORS),
        "areas": agent.normalize_areas(AREAS),
        "devices": agent.normalize_devices(DEVICES),
        "entity_regs": agent.normalize_entity_registry(ENTITY_REGISTRY_DISPLAY),
        "loaded_at": 1.0,
    }
