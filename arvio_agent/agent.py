#!/usr/bin/env python3
"""Arvio Agent — HA peek, embedded/remote enroll, claim lab API, Home controls."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlparse

# ARVIO_DATA_DIR lets the unit tests (and lab runs outside the add-on) use a
# scratch directory instead of the Supervisor-mounted /data.
DATA = Path(os.environ.get("ARVIO_DATA_DIR") or "/data")
try:
    DATA.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
STATE = DATA / "hub.json"
LAB = DATA / "lab_store.json"
APP = Path("/app")

CLOUD = "https://cloud.arvio.systems"
SERIAL = "rpi-lab-1"
PORT = 8099
RELAY_URL = "https://relay.arvio.systems"
RELAY_TOKEN = "lab-relay-token"
AGENT_VERSION = "0.1.21"
SHARE_DIR = Path("/share/arvio")
UPDATE_REQUEST = SHARE_DIR / "update_request.json"

code = "000000"
exp = 0.0
hub_id = None
ha_ok = False
err = ""
mode = "embedded"
hub_state = "prepared"
relay_ok = False
relay_err = ""

TOKEN = ""


def read_token() -> str:
    for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    for path in (
        Path("/var/run/s6/container_environment/SUPERVISOR_TOKEN"),
        Path("/run/s6/container_environment/SUPERVISOR_TOKEN"),
    ):
        try:
            if path.is_file():
                val = path.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except OSError:
            pass
    return ""


def opts() -> None:
    global CLOUD, SERIAL, mode, RELAY_URL, RELAY_TOKEN
    p = DATA / "options.json"
    if p.exists():
        o = json.loads(p.read_text())
        raw = str(o.get("cloud_url") or CLOUD).rstrip("/")
        CLOUD = raw
        if o.get("serial"):
            SERIAL = str(o["serial"])
        if o.get("relay_url") is not None:
            RELAY_URL = str(o.get("relay_url") or "").rstrip("/")
        if o.get("relay_token"):
            RELAY_TOKEN = str(o["relay_token"])
    mode = "embedded" if CLOUD in ("", "embedded", "local") else "remote"
    # Zero-config remote: if cloud is public and relay_url empty, use same origin.
    if mode == "remote" and not RELAY_URL:
        RELAY_URL = CLOUD


def save_hub(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))


def load_hub() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def load_lab() -> dict:
    if LAB.exists():
        return json.loads(LAB.read_text())
    return {
        "orgs": {},
        "sites": {},
        "claims": {},
        "claim_tokens": {},
        "support_grants": {},
        "bootstrapped": False,
    }


def save_lab(s: dict) -> None:
    LAB.write_text(json.dumps(s, indent=2))


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def ha(path: str, method: str = "GET", body: dict | None = None, timeout: int = 10):
    global ha_ok, err, TOKEN
    if not TOKEN:
        TOKEN = read_token()
    if not TOKEN:
        err = "missing SUPERVISOR_TOKEN"
        ha_ok = False
        return None
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://supervisor/core/api{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ha_ok = True
            raw = r.read().decode()
            if not err.startswith("enroll:") and not err.startswith("heartbeat:"):
                err = ""
            return json.loads(raw) if raw else {}
    except Exception as e:
        ha_ok = False
        err = str(e)
        return None


def ha_or_raise(path: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
    """Like ha(), but raises with Core error body when available."""
    global TOKEN, err
    if not TOKEN:
        TOKEN = read_token()
    if not TOKEN:
        raise RuntimeError("missing SUPERVISOR_TOKEN")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://supervisor/core/api{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            detail = str(e)
        err = detail or str(e)
        raise RuntimeError(f"HA {method} {path}: {err}") from e
    except Exception as e:
        err = str(e)
        raise RuntimeError(f"HA {method} {path}: {e}") from e


ARVIO_SCENARIO_PREFIX = "arvio_"


def _is_arvio_scenario_id(sid: str) -> bool:
    return sid.startswith(ARVIO_SCENARIO_PREFIX) and len(sid) > len(
        ARVIO_SCENARIO_PREFIX
    )


def list_scene_entities() -> list:
    states = ha("/states")
    if not isinstance(states, list):
        return []
    out = []
    for e in states:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entity_id") or "")
        if not eid.startswith("scene."):
            continue
        attrs = e.get("attributes") if isinstance(e.get("attributes"), dict) else {}
        out.append(
            {
                "entity_id": eid,
                "name": str(attrs.get("friendly_name") or eid),
                "state": str(e.get("state") or ""),
            }
        )
    return out[:80]


def list_scenarios() -> dict:
    """List Arvio-managed HA automations + scene picker entities."""
    states = ha("/states")
    if not isinstance(states, list):
        raise RuntimeError(err or "HA states failed")
    scenarios = []
    for e in states:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entity_id") or "")
        if not eid.startswith("automation."):
            continue
        attrs = e.get("attributes") if isinstance(e.get("attributes"), dict) else {}
        sid = str(attrs.get("id") or "")
        if not _is_arvio_scenario_id(sid):
            continue
        cfg = ha(f"/config/automation/config/{sid}", timeout=15)
        enabled = str(e.get("state") or "") != "off"
        item = {
            "id": sid,
            "entity_id": eid,
            "alias": str(
                (cfg.get("alias") if isinstance(cfg, dict) else None)
                or attrs.get("friendly_name")
                or sid
            ),
            "enabled": enabled,
            "last_triggered": attrs.get("last_triggered"),
            "config": cfg if isinstance(cfg, dict) else None,
        }
        scenarios.append(item)
    extras: dict = {}
    try:
        extras["devices"] = list_devices().get("devices") or []
    except Exception as e:
        extras["devices"] = []
        extras["devices_error"] = str(e)
    try:
        extras["blueprints"] = list_blueprints().get("blueprints") or []
    except Exception as e:
        extras["blueprints"] = []
        extras["blueprints_error"] = str(e)
    try:
        extras["node_red"] = node_red_status()
    except Exception as e:
        extras["node_red"] = {"ok": False, "installed": False, "error": str(e)}
    return {
        "ok": True,
        "scenarios": scenarios,
        "scenes": list_scene_entities(),
        **extras,
    }


class HaWs:
    """One authenticated Home Assistant websocket connection (documented WS API).

    Reused for several commands in a row (registry reads, subscriptions) so the
    model builder does not pay auth four times. Not thread-safe: one owner.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        try:
            import websocket  # type: ignore
        except ImportError as e:
            raise RuntimeError("websocket-client not installed") from e
        if not TOKEN:
            raise RuntimeError("missing SUPERVISOR_TOKEN")
        self._ws = websocket.create_connection(
            "ws://supervisor/core/websocket", timeout=timeout
        )
        self._next_id = 1
        try:
            hello = json.loads(self._ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"unexpected HA ws hello: {hello}")
            self._ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
            auth = json.loads(self._ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"HA ws auth failed: {auth}")
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "HaWs":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def send(self, msg_type: str, extra: dict | None = None) -> int:
        """Send one command frame; returns its id."""
        mid = self._next_id
        self._next_id += 1
        payload = {"id": mid, "type": msg_type}
        if extra:
            payload.update(extra)
        self._ws.send(json.dumps(payload))
        return mid

    def recv(self) -> dict:
        data = json.loads(self._ws.recv())
        return data if isinstance(data, dict) else {}

    def settimeout(self, timeout: float | None) -> None:
        self._ws.settimeout(timeout)

    def command(self, msg_type: str, extra: dict | None = None):
        """Send a command and wait for its result (frames for other ids are dropped)."""
        mid = self.send(msg_type, extra)
        while True:
            data = self.recv()
            if data.get("id") != mid:
                continue
            if data.get("type") == "result":
                if not data.get("success"):
                    err_obj = data.get("error") or {}
                    raise RuntimeError(
                        str(err_obj.get("message") or err_obj or "ws command failed")
                    )
                return data.get("result")
            raise RuntimeError(f"unexpected HA ws response: {data}")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def ha_ws_command(msg_type: str, extra: dict | None = None, timeout: float = 20.0):
    """Call one Home Assistant websocket command on a fresh connection."""
    with HaWs(timeout=timeout) as ws:
        return ws.command(msg_type, extra)


class HaWsUnavailable(RuntimeError):
    """No HA websocket could be opened (token / library / HA down) — nothing was sent."""


HA_CMD_WS_IDLE_PING_S = 30.0


class SharedHaWs:
    """One lazily opened HA websocket shared by callers of the same kind, serialised by its own lock.

    Two channels exist (`_HA_CMD_CHANNEL`, `_HA_QUERY_CHANNEL`) so that a slow read-only
    round trip (Spotify / Music Assistant browse or search, up to the socket timeout)
    never holds up a `call_service` for a light, lock or player — and vice versa.
    `HaWs.command` drops frames of other ids, so one socket cannot be multiplexed.
    """

    def __init__(self, name: str, timeout: float = 20.0) -> None:
        self.name = name
        self.timeout = timeout
        self.ws: HaWs | None = None
        self.used_at = 0.0
        self.lock = threading.Lock()

    def drop(self) -> None:
        """Close and forget the socket (the next command reopens it)."""
        with self.lock:
            ws, self.ws = self.ws, None
        if ws is not None:
            ws.close()

    def command(self, msg_type: str, extra: dict | None = None):
        """Run one WS command on the channel's connection (see `ha_ws_shared_command`)."""
        with self.lock:
            ws = self.ws
            if ws is not None and time.time() - self.used_at > HA_CMD_WS_IDLE_PING_S:
                try:
                    ws.command("ping")
                except Exception:
                    ws.close()
                    ws = self.ws = None
            if ws is None:
                try:
                    ws = HaWs(timeout=self.timeout)
                except Exception as e:
                    raise HaWsUnavailable(str(e)) from e
                self.ws = ws
            try:
                result = ws.command(msg_type, extra)
            except Exception:
                self.ws = None
                ws.close()
                raise
            self.used_at = time.time()
            return result


# `call_service` that changes state (lights, locks, players) — kept short and never queued
# behind a browse; read-only lookups (browse / search / `return_response` reads) go through
# `_HA_QUERY_CHANNEL`, so each channel only waits on its own kind.
_HA_CMD_CHANNEL = SharedHaWs("cmd")
_HA_QUERY_CHANNEL = SharedHaWs("query")


def ha_ws_shared_command(msg_type: str, extra: dict | None = None):
    """Run one WS command on the lazily opened, shared **command** HA connection.

    Raises HaWsUnavailable only when no connection can be established, i.e. before
    anything was sent (the caller may fall back to REST without double execution).
    Any failure after sending propagates; the socket is dropped and reopened next time.
    A socket idle for a while is probed with a documented `ping` first, so a silently
    dead connection is replaced before the real command goes out.
    """
    return _HA_CMD_CHANNEL.command(msg_type, extra)


def ha_ws_query_command(msg_type: str, extra: dict | None = None):
    """Same semantics as `ha_ws_shared_command`, on the separate read-only **query**
    connection (browse_media / search_media / `return_response` reads) with its own lock —
    a slow media round trip blocks other queries only, never a `call_service`."""
    return _HA_QUERY_CHANNEL.command(msg_type, extra)


def ha_call_service(domain: str, service: str, data: dict) -> str | None:
    """Call a HA service and return the call's `context.id` (None when unknown).

    Documented WS `call_service` first — its result always carries `context`, so the
    later `state_changed` of slow (Zigbee) devices can still be mapped to the command.
    REST `POST /api/services/{domain}/{service}` when no WS can be opened; there the
    context comes from the changed states in the response (empty when nothing changed).
    """
    try:
        result = ha_ws_shared_command(
            "call_service", {"domain": domain, "service": service, "service_data": data}
        )
    except HaWsUnavailable:
        result = ha(f"/services/{domain}/{service}", method="POST", body=data)
        if result is None:
            raise RuntimeError(err or "HA service failed")
        return _context_id_from_service_result(result)
    ctx = result.get("context") if isinstance(result, dict) else None
    cid = ctx.get("id") if isinstance(ctx, dict) else None
    return str(cid) if cid else None


def config_entries() -> list:
    """GET /api/config/config_entries/entry (documented Core API) → [] when unreachable."""
    entries = ha("/config/config_entries/entry")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def config_entry_for_domain(entries: list, domain: str) -> dict | None:
    """First non-disabled config entry of `domain` (pure)."""
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or e.get("domain") != domain or e.get("disabled_by"):
            continue
        return e
    return None


# Music Assistant (0.1.21 §15): the config entry id feeds `music_assistant.search`
# and the model's `ma_available`; refreshed on every arvio.model read.
MA_INFO: dict = {"entry_id": None, "checked_at": 0.0}


def music_assistant_entry_id(entries: list | None = None) -> str | None:
    """Config entry id of Music Assistant (None → the app runs «έλεγχο μόνο»). Cached in MA_INFO."""
    entry = config_entry_for_domain(config_entries() if entries is None else entries, "music_assistant")
    entry_id = str(entry["entry_id"]) if isinstance(entry, dict) and entry.get("entry_id") else None
    MA_INFO["entry_id"] = entry_id
    MA_INFO["checked_at"] = time.time()
    return entry_id


def ha_call_service_response(domain: str, service: str, data: dict) -> tuple[str | None, dict | None]:
    """call_service with a response (documented WS `return_response: true`; REST `?return_response`).

    → (context_id, response dict | None). Used for `music_assistant.search` / `get_queue`.
    Read-only, so it rides the query channel (`ha_ws_query_command`), not the command one.
    """
    try:
        result = ha_ws_query_command(
            "call_service",
            {"domain": domain, "service": service, "service_data": data, "return_response": True},
        )
    except HaWsUnavailable:
        result = ha(f"/services/{domain}/{service}?return_response", method="POST", body=data)
        if result is None:
            raise RuntimeError(err or "HA service failed")
        if isinstance(result, dict):
            resp = result.get("service_response")
            return (
                _context_id_from_service_result(result.get("changed_states")),
                resp if isinstance(resp, dict) else None,
            )
        return _context_id_from_service_result(result), None
    if not isinstance(result, dict):
        return None, None
    ctx = result.get("context") if isinstance(result.get("context"), dict) else {}
    resp = result.get("response")
    return (str(ctx["id"]) if ctx.get("id") else None), (resp if isinstance(resp, dict) else None)


def zha_is_available() -> bool:
    """True when a non-disabled ZHA config entry exists (documented Core API)."""
    if config_entry_for_domain(config_entries(), "zha") is not None:
        return True
    # Fallback: any device with zha identifier (hub may have joined devices).
    try:
        result = ha_ws_command("config/device_registry/list")
        devices = result if isinstance(result, list) else []
        for d in devices:
            if not isinstance(d, dict):
                continue
            for ident in d.get("identifiers") or []:
                if isinstance(ident, (list, tuple)) and ident and str(ident[0]) == "zha":
                    return True
    except Exception:
        pass
    return False


def pairing_status() -> dict:
    """Installer pairing readiness — Zigbee/ZHA MVP."""
    ents = entities()
    try:
        devs = list_devices().get("devices") or []
    except Exception:
        devs = []
    zha = zha_is_available()
    return {
        "ok": True,
        "zha_available": zha,
        "radio": "zigbee" if zha else None,
        "device_count": len(devs) if isinstance(devs, list) else 0,
        "entity_count": len(ents) if isinstance(ents, list) else 0,
        "agent_version": AGENT_VERSION,
    }


def zigbee_permit(payload: dict | None = None) -> dict:
    """Open ZHA network via documented action zha.permit (HA Core)."""
    payload = payload if isinstance(payload, dict) else {}
    if not zha_is_available():
        return {
            "ok": False,
            "error": "ZHA not configured — χρειάζεται Zigbee stick (π.χ. ZBT-2) και integration ZHA",
            "zha_available": False,
        }
    duration = payload.get("duration")
    try:
        duration_i = int(duration) if duration is not None else 120
    except (TypeError, ValueError):
        duration_i = 120
    duration_i = max(0, min(254, duration_i))
    body: dict = {"duration": duration_i}
    # Optional documented fields (install code / QR) — pass through when present.
    for key in ("ieee", "source_ieee", "install_code", "qr_code"):
        if payload.get(key):
            body[key] = str(payload[key])
    result = ha("/services/zha/permit", method="POST", body=body)
    if result is None:
        raise RuntimeError(err or "zha.permit failed")
    return {
        "ok": True,
        "zha_available": True,
        "duration": duration_i,
        "opened_at": time.time(),
        "service": "zha.permit",
    }


def list_devices() -> dict:
    """Device registry entries useful for device / Zigbee button triggers."""
    result = ha_ws_command("config/device_registry/list")
    devices = result if isinstance(result, list) else []
    out = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        identifiers = d.get("identifiers") or []
        # Prefer Zigbee / MQTT remotes & buttons.
        manufacturers = str(d.get("manufacturer") or "").lower()
        model = str(d.get("model") or "").lower()
        name = str(d.get("name_by_user") or d.get("name") or "")
        entry = {
            "device_id": str(d.get("id") or ""),
            "name": name,
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
            "area_id": d.get("area_id"),
            "identifiers": identifiers,
            "via_device_id": d.get("via_device_id"),
        }
        # Heuristic: mark likely buttons/remotes for Partner picker.
        blob = f"{name} {manufacturers} {model}".lower()
        entry["likely_button"] = any(
            k in blob
            for k in (
                "button",
                "remote",
                "switch",
                "dimmer",
                "cube",
                "aqara",
                "ikea",
                "tradfri",
                "hue",
            )
        )
        domains = set()
        for ident in identifiers:
            if isinstance(ident, (list, tuple)) and ident:
                domains.add(str(ident[0]))
        entry["integration_hints"] = sorted(domains)
        if entry["device_id"]:
            out.append(entry)
    out.sort(key=lambda x: (not x.get("likely_button"), str(x.get("name") or "")))
    return {"ok": True, "devices": out[:200]}


def list_blueprints() -> dict:
    """List automation blueprints via HA websocket."""
    result = ha_ws_command("blueprint/list", {"domain": "automation"})
    blueprints = []
    if isinstance(result, dict):
        for path, meta in result.items():
            item = {"path": str(path)}
            if isinstance(meta, dict):
                md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else meta
                if isinstance(md, dict):
                    item["name"] = str(md.get("name") or path)
                    item["description"] = str(md.get("description") or "")
                    item["input"] = md.get("input") if isinstance(md.get("input"), dict) else {}
                else:
                    item["name"] = str(path)
            else:
                item["name"] = str(path)
            blueprints.append(item)
    elif isinstance(result, list):
        for b in result:
            if isinstance(b, dict):
                blueprints.append(b)
    return {"ok": True, "blueprints": blueprints}


def node_red_status() -> dict:
    """Detect Node-RED Supervisor add-on (not a Node-RED clone)."""
    resp = supervisor("/addons", "GET", timeout=30)
    addons = []
    if isinstance(resp, dict):
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        raw = data.get("addons") if isinstance(data, dict) else None
        if isinstance(raw, list):
            addons = raw
    match = None
    for a in addons:
        if not isinstance(a, dict):
            continue
        slug = str(a.get("slug") or "").lower()
        name = str(a.get("name") or "").lower()
        if "node-red" in slug or "nodered" in slug.replace("_", "") or "node-red" in name:
            match = a
            break
    if not match:
        return {
            "ok": True,
            "installed": False,
            "running": False,
            "slug": None,
            "ingress_url": None,
            "note": "Install Node-RED from Supervisor add-on store for advanced flows",
        }
    slug = str(match.get("slug") or "")
    state = str(match.get("state") or "")
    ingress = bool(match.get("ingress"))
    # Ingress path is typically /api/hassio_ingress/<token> — Partner cannot open
    # Supervisor UI remotely; return slug for installer guidance.
    return {
        "ok": True,
        "installed": True,
        "running": state == "started",
        "slug": slug,
        "name": match.get("name"),
        "version": match.get("version"),
        "ingress": ingress,
        "ingress_url": None,
        "note": (
            "Node-RED is on this hub — open it from HA Supervisor → Node-RED. "
            "Arvio scenarios stay as native HA automations."
        ),
    }


def upsert_scenario(payload: dict) -> dict:
    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if not isinstance(cfg, dict):
        raise ValueError("config required")
    sid = str(cfg.get("id") or "").strip()
    if not _is_arvio_scenario_id(sid):
        raise ValueError("scenario id must start with arvio_")
    body = dict(cfg)
    body["id"] = sid
    if "mode" not in body:
        body["mode"] = "single"
    ha_or_raise(
        f"/config/automation/config/{sid}",
        method="POST",
        body=body,
        timeout=45,
    )
    verify = ha(f"/config/automation/config/{sid}", timeout=15)
    if not isinstance(verify, dict):
        raise RuntimeError("upsert verify failed")
    return {"ok": True, "id": sid, "config": verify}


def delete_scenario(payload: dict, entity_id: str = "") -> dict:
    sid = str(payload.get("id") or entity_id or "").strip()
    if not _is_arvio_scenario_id(sid):
        raise ValueError("scenario id must start with arvio_")
    ha_or_raise(
        f"/config/automation/config/{sid}",
        method="DELETE",
        timeout=45,
    )
    return {"ok": True, "id": sid, "deleted": True}


def _resolve_automation_entity(sid: str, entity_id: str = "") -> str:
    """Scenario id → automation entity_id. Only `arvio_*` ids (like delete_scenario); an explicit
    automation entity_id is verified via /states (`attributes.id == sid`) so a caller cannot
    trigger / toggle a foreign automation by naming it."""
    if not _is_arvio_scenario_id(sid):
        raise ValueError("scenario id must start with arvio_")
    eid = str(entity_id or "").strip()
    if eid.startswith("automation."):
        st_obj = ha(f"/states/{eid}")
        attrs = (
            st_obj.get("attributes")
            if isinstance(st_obj, dict) and isinstance(st_obj.get("attributes"), dict)
            else {}
        )
        if str(attrs.get("id") or "") != sid:
            raise ValueError("automation entity_id does not belong to scenario")
        return eid
    states = ha("/states")
    if isinstance(states, list):
        for e in states:
            if not isinstance(e, dict):
                continue
            attrs = e.get("attributes") if isinstance(e.get("attributes"), dict) else {}
            if str(attrs.get("id") or "") == sid:
                found = str(e.get("entity_id") or "")
                if found.startswith("automation."):
                    return found
    raise ValueError("automation entity_id required")


def set_scenario_enabled(payload: dict, entity_id: str = "") -> dict:
    sid = str(payload.get("id") or "").strip()
    eid = _resolve_automation_entity(sid, str(payload.get("entity_id") or entity_id or ""))
    enabled = bool(payload.get("enabled", True))
    service = "turn_on" if enabled else "turn_off"
    out = call_service("automation", service, {"entity_id": eid})
    out["id"] = sid
    out["enabled"] = enabled
    return out


def trigger_scenario(payload: dict, entity_id: str = "") -> dict:
    sid = str(payload.get("id") or "").strip()
    eid = _resolve_automation_entity(sid, str(payload.get("entity_id") or entity_id or ""))
    out = call_service("automation", "trigger", {"entity_id": eid})
    out["id"] = sid
    return out


def supervisor(path: str, method: str = "GET", body: dict | None = None, timeout: int = 120):
    """Home Assistant Supervisor API (not Core). Documented /backups endpoints."""
    global TOKEN, err
    if not TOKEN:
        TOKEN = read_token()
    if not TOKEN:
        err = "missing SUPERVISOR_TOKEN"
        return None
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://supervisor{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except Exception as e:
        err = f"supervisor:{e}"
        return None


def list_local_backups() -> list:
    resp = supervisor("/backups", "GET", timeout=30)
    if not isinstance(resp, dict):
        return []
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    backups = data.get("backups") if isinstance(data, dict) else None
    if not isinstance(backups, list):
        return []
    out = []
    for b in backups:
        if not isinstance(b, dict):
            continue
        out.append(
            {
                "slug": str(b.get("slug") or ""),
                "name": str(b.get("name") or b.get("slug") or ""),
                "date": str(b.get("date") or ""),
                "size_bytes": b.get("size_bytes"),
                "protected": bool(b.get("protected")),
            }
        )
    return [b for b in out if b["slug"]]


def create_full_backup(name: str | None = None) -> dict:
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    body = {
        "name": name or f"Arvio {stamp}",
        "compressed": True,
        "background": True,
    }
    resp = supervisor("/backups/new/full", "POST", body, timeout=30)
    if not isinstance(resp, dict):
        raise RuntimeError(err or "backup create failed")
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    if not isinstance(data, dict):
        raise RuntimeError("backup create: unexpected response")
    if resp.get("result") == "error":
        raise RuntimeError(str(data.get("message") or "backup create error"))
    return {
        "ok": True,
        "slug": data.get("slug"),
        "job_id": data.get("job_id"),
        "name": body["name"],
    }


def backup_telemetry() -> dict:
    backups = list_local_backups()
    newest = None
    for b in backups:
        d = b.get("date") or ""
        if not d:
            continue
        if newest is None or d > newest:
            newest = d
    return {
        "backups": backups,
        "backup_count": len(backups),
        "last_backup_at": newest,
        "agent_version": AGENT_VERSION,
    }


def addon_self_info() -> dict:
    resp = supervisor("/addons/self/info", "GET", timeout=20)
    if not isinstance(resp, dict):
        return {}
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    return data if isinstance(data, dict) else {}


def find_agent_update_entity(slug: str) -> str | None:
    """Locate Core update.* entity for this add-on (documented update.install path)."""
    states = ha("/states")
    if not isinstance(states, list):
        return None
    slug_l = (slug or "").lower()
    for e in states:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entity_id") or "")
        if not eid.startswith("update."):
            continue
        attrs = e.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        title = str(attrs.get("title") or attrs.get("friendly_name") or "").lower()
        # Hassio entities often embed slug in unique_id / entity_id.
        blob = f"{eid} {title} {attrs.get('installed_version','')}".lower()
        if "arvio" in title or "arvio_agent" in eid or (slug_l and slug_l in blob):
            return eid
    return None


def write_updater_request(slug: str, target: str, channel: str) -> None:
    """Handoff for companion arvio_updater (Supervisor forbids self-update)."""
    try:
        SHARE_DIR.mkdir(parents=True, exist_ok=True)
        UPDATE_REQUEST.write_text(
            json.dumps(
                {
                    "slug": slug,
                    "target_version": target or None,
                    "channel": channel or None,
                    "requested_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                    "requested_by": "arvio_agent",
                    "agent_version": AGENT_VERSION,
                },
                indent=2,
            )
        )
    except OSError as e:
        raise RuntimeError(f"cannot write update request: {e}") from e


def apply_agent_update(payload: dict) -> dict:
    """
    Remote Agent OTA (ARV-060).
    Documented paths:
    - Core update.install (preferred; REQUEST_FROM is Core, not self)
    - Companion handoff via /share/arvio/update_request.json
    - Direct POST /store/addons/{slug}/update is forbidden for self (403)
    """
    target = str(payload.get("target_version") or payload.get("version") or "")
    channel = str(payload.get("channel") or "")
    do_backup = payload.get("backup", True) is not False

    st = load_hub()
    if target:
        st["agent_target_version"] = target
    if channel:
        st["update_channel"] = channel
    save_hub(st)

    info = addon_self_info()
    slug = str(info.get("slug") or "local_arvio_agent")
    version = str(info.get("version") or AGENT_VERSION)
    version_latest = str(info.get("version_latest") or "")
    update_available = bool(info.get("update_available"))

    # Refresh store so version_latest is current (repository installs).
    supervisor("/store/reload", "POST", {}, timeout=60)

    info2 = addon_self_info()
    if info2:
        info = info2
        version = str(info.get("version") or version)
        version_latest = str(info.get("version_latest") or version_latest)
        update_available = bool(info.get("update_available"))
        slug = str(info.get("slug") or slug)

    write_updater_request(slug, target, channel)

    result: dict = {
        "ok": True,
        "pinned": True,
        "slug": slug,
        "agent_version": AGENT_VERSION,
        "installed_version": version,
        "version_latest": version_latest or None,
        "update_available": update_available,
        "agent_target_version": st.get("agent_target_version"),
        "update_channel": st.get("update_channel"),
        "updater_request": str(UPDATE_REQUEST),
    }

    # Preferred: Core update.install → Supervisor (not self-call).
    entity_id = find_agent_update_entity(slug)
    if entity_id and (update_available or target):
        body: dict = {"entity_id": entity_id, "backup": do_backup}
        if target:
            body["version"] = target
        svc = ha("/services/update/install", method="POST", body=body)
        result["core_update"] = {
            "entity_id": entity_id,
            "requested": True,
            "response": svc is not None,
        }
        result["method"] = "core_update.install"
        result["note"] = (
            "update.install requested; add-on will restart if Supervisor applies it"
        )
        return result

    # Always leave companion request; report status for local/lab installs.
    result["method"] = "companion_handoff"
    if not update_available and not version_latest:
        result["note"] = (
            "no store update available (local install?) — "
            "arvio_updater will try /store/addons/{slug}/update; "
            "lab local builds need rebuild/reinstall"
        )
    else:
        result["note"] = (
            "wrote /share/arvio/update_request.json for arvio_updater companion"
        )
    return result


def rotate() -> None:
    global code, exp
    code = f"{random.randint(0, 999999):06d}"
    exp = time.time() + 300


def enroll_embedded() -> None:
    global hub_id, err, hub_state
    st = load_hub()
    pk = st.get("enroll_public_key") or secrets.token_hex(8)
    hid = st.get("hub_id") or f"hub_lab_{SERIAL.replace('-', '_')}"
    hub_state = st.get("state") or "prepared"
    st.update(
        {
            "enroll_public_key": pk,
            "serial": SERIAL,
            "hub_id": hid,
            "state": hub_state,
            "mode": "embedded",
            "presence_code_hash": sha(code),
            "presence_expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)
            ),
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    save_hub(st)
    hub_id = hid
    if err.startswith("enroll:") or err.startswith("heartbeat:"):
        err = ""


def cloud_json(method: str, path: str, body: dict | None = None) -> dict:
    """Proxy JSON request to Arvio cloud (remote mode)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{CLOUD}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            out = json.loads(raw) if raw else {}
            if isinstance(out, dict) and out.get("error"):
                raise ValueError(str(out["error"]))
            return out
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
            msg = err_body.get("error") or str(e)
        except Exception:
            msg = str(e)
        raise ValueError(msg) from e


def cache_lab_boot(boot: dict) -> None:
    lab = load_lab()
    lab["bootstrapped"] = True
    lab["partner_org_id"] = boot.get("partner_org_id")
    lab["site_id"] = boot.get("site_id")
    lab["customer_org_id"] = boot.get("customer_org_id")
    save_lab(lab)


def sync_hub_from_cloud(hub: dict) -> None:
    global hub_state, hub_id
    st = load_hub()
    st["state"] = hub.get("state") or st.get("state")
    st["site_id"] = hub.get("site_id")
    st["customer_org_id"] = hub.get("customer_org_id")
    save_hub(st)
    hub_state = st.get("state") or hub_state
    hub_id = st.get("hub_id") or hub_id


def enroll_remote() -> None:
    global hub_id, err, hub_state
    st = load_hub()
    pk = st.get("enroll_public_key") or secrets.token_hex(8)
    st.update({"enroll_public_key": pk, "serial": SERIAL})
    save_hub(st)
    try:
        req = urllib.request.Request(
            f"{CLOUD}/v1/hubs/enroll",
            data=json.dumps(
                {"serial": SERIAL, "enroll_public_key": pk}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read().decode())
            hub_id = body.get("hub_id")
            hub_state = body.get("state") or "prepared"
            st["hub_id"] = hub_id
            st["state"] = hub_state
            save_hub(st)
    except Exception as e:
        err = f"enroll:{e}"
        hub_id = st.get("hub_id")
        return
    if not hub_id:
        return
    try:
        payload = {
            "presence_code_hash": sha(code),
            "presence_expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)
            ),
            # lets the cloud serve site_model_cache without asking the hub
            # when nothing changed (cloud home-model.ts hubSnapshotVersion)
            "snapshot_version": get_snapshot_version(),
        }
        payload.update(backup_telemetry())
        # Accept target pin from cloud response for agent.update follow-up.
        req = urllib.request.Request(
            f"{CLOUD}/v1/hubs/{hub_id}/heartbeat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode()
            try:
                hb = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                hb = {}
            st = load_hub()
            if hb.get("agent_target_version"):
                st["agent_target_version"] = hb.get("agent_target_version")
            if hb.get("update_channel"):
                st["update_channel"] = hb.get("update_channel")
            save_hub(st)
        if err.startswith("enroll:") or err.startswith("heartbeat:"):
            err = ""
    except Exception as e:
        err = f"heartbeat:{e}"


def enroll() -> None:
    if mode == "embedded":
        enroll_embedded()
    else:
        enroll_remote()


def entities() -> list:
    st = ha("/states")
    if not isinstance(st, list):
        return []
    out = []
    for e in st:
        eid = str(e.get("entity_id", ""))
        if not eid.startswith(
            (
                "light.",
                "switch.",
                "cover.",
                "climate.",
                "lock.",
                "alarm_control_panel.",
                "media_player.",
            )
        ):
            continue
        attrs = e.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        if eid.startswith("media_player.") and not media_player_exposed(attrs.get("device_class")):
            continue
        color_modes = attrs.get("supported_color_modes") or []
        if not isinstance(color_modes, list):
            color_modes = []
        domain = eid.split(".", 1)[0]
        caps = {
            "brightness": domain == "light"
            and (
                "brightness" in color_modes
                or attrs.get("brightness") is not None
                or attrs.get("brightness_pct") is not None
            ),
            "color": domain == "light"
            and any(m in color_modes for m in ("rgb", "rgbw", "rgbww", "hs", "xy")),
            "color_temp": domain == "light"
            and (
                "color_temp" in color_modes or attrs.get("color_temp") is not None
            ),
            "position": domain == "cover"
            and attrs.get("current_position") is not None,
            "temperature": domain == "climate",
            "preset": domain == "climate"
            and bool(attrs.get("preset_modes")),
            "swing": domain == "climate"
            and bool(attrs.get("swing_modes")),
            "fan": domain == "climate" and bool(attrs.get("fan_modes")),
        }
        item = {
            "entity_id": eid,
            "state": e.get("state"),
            "name": attrs.get("friendly_name", eid),
            "domain": domain,
            "capabilities": caps,
            "brightness": attrs.get("brightness"),
            "brightness_pct": attrs.get("brightness_pct"),
            "rgb_color": attrs.get("rgb_color"),
            "color_temp": attrs.get("color_temp"),
            "supported_color_modes": color_modes,
            "current_temperature": attrs.get("current_temperature"),
            "temperature": attrs.get("temperature"),
            "target_temp_high": attrs.get("target_temp_high"),
            "target_temp_low": attrs.get("target_temp_low"),
            "hvac_mode": attrs.get("hvac_mode") or e.get("state"),
            "hvac_modes": attrs.get("hvac_modes") or [],
            "fan_mode": attrs.get("fan_mode"),
            "fan_modes": attrs.get("fan_modes") or [],
            "preset_mode": attrs.get("preset_mode"),
            "preset_modes": attrs.get("preset_modes") or [],
            "swing_mode": attrs.get("swing_mode"),
            "swing_modes": attrs.get("swing_modes") or [],
            "min_temp": attrs.get("min_temp"),
            "max_temp": attrs.get("max_temp"),
            "current_position": attrs.get("current_position"),
            "unit_of_measurement": attrs.get("unit_of_measurement"),
        }
        if domain == "media_player":
            state_s = str(e.get("state")) if e.get("state") is not None else None
            item.update(typed_attrs("media_player", state_s, attrs))
        out.append(item)
    return out[:120]


def _expected_state_for(domain: str, service: str) -> str | None:
    if domain == "media_player":
        # play/pause/stop/on/off confirm via state; next/previous/volume/source via attributes
        # (media_expected_attrs) or not at all (play_pause toggles — no expectation).
        return {
            "media_play": "playing",
            "media_pause": "paused",
            "media_stop": "idle",
            "turn_on": "on",
            "turn_off": "off",
        }.get(service)
    if domain == "music_assistant":
        return None
    # Scenes/scripts are stateless (scene state = timestamp, script flips back to off).
    if service == "turn_on" and domain not in ("scene", "script"):
        return "on"
    if service == "turn_off" and domain not in ("scene", "script"):
        return "off"
    if service == "open_cover":
        return "open"
    if service == "close_cover":
        return "closed"
    if domain == "lock" and service == "lock":
        return "locked"
    if domain == "lock" and service == "unlock":
        return "unlocked"
    if domain == "alarm_control_panel":
        return {
            "alarm_arm_home": "armed_home",
            "alarm_arm_away": "armed_away",
            "alarm_arm_night": "armed_night",
            "alarm_disarm": "disarmed",
        }.get(service)
    return None


def _state_matches(domain: str, expected: str | None, state: str | None) -> bool:
    if expected is None or state is None:
        return True
    if domain == "climate" and expected == "on":
        return state not in ("off", "unavailable", "unknown")
    if domain == "climate" and expected == "off":
        return state == "off"
    if domain == "alarm_control_panel" and expected.startswith("armed_"):
        # Exit delay: "arming" is progress, the final state arrives later (§6.7).
        return state in (expected, "arming")
    if domain == "lock":
        return state == expected or state in ("locking", "unlocking", "jammed")
    if domain == "media_player":
        if expected == "playing":
            return state in ("playing", "buffering")
        if expected == "idle":
            return state not in ("playing", "buffering")
        if expected == "on":
            return state not in ("off", "unavailable", "unknown", "standby")
        if expected == "off":
            return state in ("off", "standby")
    return state == expected


MEDIA_VOLUME_TOLERANCE = 0.02


def _attrs_match(state_obj, expected_attrs: dict | None) -> bool:
    """Attribute expectations (volume_set / select_source / mute / shuffle / repeat). Pure."""
    if not expected_attrs:
        return True
    if not isinstance(state_obj, dict):
        return False
    attrs = state_obj.get("attributes") if isinstance(state_obj.get("attributes"), dict) else {}
    for key, want in expected_attrs.items():
        have = attrs.get(key)
        if key == "volume_level":
            h, w = _num(have), _num(want)
            if h is None or w is None or abs(float(h) - float(w)) > MEDIA_VOLUME_TOLERANCE:
                return False
        elif isinstance(want, list):
            if not isinstance(have, list) or {str(x) for x in have} != {str(x) for x in want}:
                return False
        elif have != want:
            return False
    return True


def _state_of(state_obj) -> str | None:
    if not isinstance(state_obj, dict) or state_obj.get("state") is None:
        return None
    return str(state_obj.get("state"))


def _context_id_from_service_result(result) -> str | None:
    """REST /api/services/... returns the changed states; each carries the call's context."""
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and isinstance(item.get("context"), dict):
                cid = item["context"].get("id")
                if cid:
                    return str(cid)
    elif isinstance(result, dict) and isinstance(result.get("context"), dict):
        cid = result["context"].get("id")
        if cid:
            return str(cid)
    return None


def _snapshot_entry(state_obj: dict | None) -> dict:
    if not isinstance(state_obj, dict):
        return {"state": None, "attrs": {}}
    eid = str(state_obj.get("entity_id") or "")
    domain = eid.split(".", 1)[0] if "." in eid else ""
    attrs = state_obj.get("attributes") if isinstance(state_obj.get("attributes"), dict) else {}
    state = state_obj.get("state")
    state = str(state) if state is not None else None
    platform = None
    if domain == "media_player":
        reg = registry_snapshot().entity_regs.get(eid)
        platform = reg.get("platform") if isinstance(reg, dict) else None
    return {
        "state": state,
        "last_changed": state_obj.get("last_changed"),
        "attrs": typed_attrs(domain, state, attrs, platform=platform) if domain else {},
    }


def call_service(
    domain: str,
    service: str,
    data,
    wait_entity_ids: list | None = None,
    expected_attrs: dict | None = None,
) -> dict:
    """Call a documented HA service, then wait briefly for the state to settle (0.1.13).

    Returns the legacy flat fields plus `state_snapshot`, `affected_entity_ids`, `ha_context_id`.
    0.1.20 (§14.3): no "expected state" fallback — the snapshot is what HA reports, and
    `ok` is False (`error: "state_not_confirmed"`) when HA did not confirm within the wait.
    0.1.21: `expected_attrs` confirms via attributes (media volume_set / select_source …).
    """
    if isinstance(data, str):
        data = {"entity_id": data}
    data = dict(data) if isinstance(data, dict) else {}
    sent_at = time.time()
    ha_context_id = ha_call_service(domain, service, data)

    raw_ids = data.get("entity_id")
    if wait_entity_ids:
        entity_ids = [str(x) for x in wait_entity_ids]
    elif isinstance(raw_ids, list):
        entity_ids = [str(x) for x in raw_ids]
    elif raw_ids:
        entity_ids = [str(raw_ids)]
    else:
        entity_ids = []
    entity_id = entity_ids[0] if len(entity_ids) == 1 else ""

    expected = _expected_state_for(domain, service)
    states = wait_for_states(domain, expected, entity_ids, sent_at=sent_at, expected_attrs=expected_attrs)
    settled = all(
        _state_matches(domain, expected, _state_of(states.get(e))) and _attrs_match(states.get(e), expected_attrs)
        for e in entity_ids
    )

    confirmed = (expected is None and not expected_attrs) or not entity_ids or settled
    snapshot_entities: dict = {}
    for e in entity_ids[:BATCH_MAX_CALLS]:
        snapshot_entities[e] = _snapshot_entry(states.get(e))

    primary = snapshot_entities.get(entity_id) if entity_id else None
    primary_state = primary["state"] if primary else None
    primary_attrs = states.get(entity_id, {}).get("attributes") if entity_id and isinstance(states.get(entity_id), dict) else {}
    if not isinstance(primary_attrs, dict):
        primary_attrs = {}
    if entity_id:
        state_snapshot: dict = {"entity_id": entity_id, **(primary or {"state": None, "attrs": {}})}
    else:
        state_snapshot = {"entities": snapshot_entities}

    return {
        "ok": confirmed,
        "error": None if confirmed else "state_not_confirmed",
        "entity_id": entity_id,
        "service": f"{domain}.{service}",
        "state": primary_state,
        "brightness": primary_attrs.get("brightness"),
        "rgb_color": primary_attrs.get("rgb_color"),
        "temperature": primary_attrs.get("temperature"),
        "hvac_mode": primary_attrs.get("hvac_mode"),
        "fan_mode": primary_attrs.get("fan_mode"),
        "preset_mode": primary_attrs.get("preset_mode"),
        "swing_mode": primary_attrs.get("swing_mode"),
        "current_position": primary_attrs.get("current_position"),
        "volume_level": primary_attrs.get("volume_level"),
        "state_snapshot": state_snapshot,
        "affected_entity_ids": entity_ids,
        "ha_context_id": ha_context_id,
    }


def build_service_data(action: str, entity_id: str, payload: dict, ha_version=None) -> tuple[str, dict]:
    """Payload → HA service data for one entity (pure). Returns (service, data)."""
    domain, service = action.split(".", 1)
    data: dict = {"entity_id": entity_id} if entity_id else {}
    if action == "light.turn_on":
        if payload.get("brightness") is not None:
            data["brightness"] = int(payload["brightness"])
        if payload.get("brightness_pct") is not None:
            data["brightness_pct"] = int(payload["brightness_pct"])
        if payload.get("rgb_color") is not None:
            data["rgb_color"] = payload["rgb_color"]
        if payload.get("hs_color") is not None:
            data["hs_color"] = payload["hs_color"]
        if payload.get("color_temp_kelvin") is not None:
            kelvin = int(payload["color_temp_kelvin"])
            if ha_version_at_least(ha_version, HA_KELVIN_MIN_VERSION):
                data["color_temp_kelvin"] = kelvin
            else:
                # HA < 2022.12 only understands mireds.
                data["color_temp"] = kelvin_to_mireds(kelvin)
        elif payload.get("color_temp") is not None:
            data["color_temp"] = int(payload["color_temp"])
    elif action == "climate.set_temperature":
        if payload.get("temperature") is not None:
            data["temperature"] = float(payload["temperature"])
        if payload.get("hvac_mode") is not None:
            data["hvac_mode"] = str(payload["hvac_mode"])
    elif action == "climate.set_hvac_mode":
        data["hvac_mode"] = str(payload.get("hvac_mode") or service)
    elif action == "climate.set_fan_mode":
        data["fan_mode"] = str(payload.get("fan_mode") or "")
    elif action == "climate.set_preset_mode":
        data["preset_mode"] = str(payload.get("preset_mode") or "")
    elif action == "climate.set_swing_mode":
        data["swing_mode"] = str(payload.get("swing_mode") or "")
    elif action == "cover.set_cover_position":
        data["position"] = int(payload.get("position") or 0)
    elif action in ("lock.lock", "lock.unlock", "lock.open") or action.startswith(
        "alarm_control_panel."
    ):
        # Optional device code, injected by the cloud only after confirm verification (§6.7).
        if payload.get("code") not in (None, ""):
            data["code"] = str(payload["code"])
    elif domain == "media_player":
        if service == "volume_set":
            if payload.get("volume_level") is None:
                raise ValueError("volume_level required")
            data["volume_level"] = clamp_volume(payload.get("volume_level"), payload.get("volume_max"))
        elif service == "volume_mute":
            data["is_volume_muted"] = bool(payload.get("is_volume_muted", True))
        elif service == "select_source":
            if not payload.get("source"):
                raise ValueError("source required")
            data["source"] = str(payload["source"])
        elif service == "join":
            members = payload.get("group_members")
            if isinstance(members, str):
                members = [members]
            members = [str(x) for x in members if x] if isinstance(members, list) else []
            if not members:
                raise ValueError("group_members required")
            data["group_members"] = members
        elif service == "play_media":
            if not payload.get("media_content_id") or not payload.get("media_content_type"):
                raise ValueError("media_content_id and media_content_type required")
            data["media_content_id"] = str(payload["media_content_id"])
            data["media_content_type"] = str(payload["media_content_type"])
            if payload.get("enqueue") is not None:
                if str(payload["enqueue"]) not in MEDIA_ENQUEUE_MODES:
                    raise ValueError("enqueue must be add|next|play|replace")
                data["enqueue"] = str(payload["enqueue"])
        elif service == "shuffle_set":
            data["shuffle"] = bool(payload.get("shuffle", True))
        elif service == "repeat_set":
            repeat = str(payload.get("repeat") or "off")
            if repeat not in MEDIA_REPEAT_MODES:
                raise ValueError("repeat must be off|all|one")
            data["repeat"] = repeat
    elif domain == "music_assistant":
        if service == "play_media":
            media_id = payload.get("media_id")
            if not media_id:
                raise ValueError("media_id required")
            data["media_id"] = [str(x) for x in media_id] if isinstance(media_id, list) else str(media_id)
            if payload.get("media_type"):
                data["media_type"] = str(payload["media_type"])
            for key in ("artist", "album"):
                if payload.get(key):
                    data[key] = str(payload[key])
            if payload.get("enqueue") is not None:
                if str(payload["enqueue"]) not in MA_ENQUEUE_MODES:
                    raise ValueError("enqueue must be play|replace|next|replace_next|add")
                data["enqueue"] = str(payload["enqueue"])
            if payload.get("radio_mode") is not None:
                data["radio_mode"] = bool(payload["radio_mode"])
        elif service == "transfer_queue":
            if payload.get("source_player"):
                data["source_player"] = str(payload["source_player"])
            if payload.get("auto_play") is not None:
                data["auto_play"] = bool(payload["auto_play"])
    return service, data


def clamp_volume(level, volume_max=None) -> float:
    """volume_level → 0–1, additionally capped by `volume_max` (cloud entity_meta, §15.5). Pure."""
    v = _num(level)
    if v is None:
        raise ValueError("volume_level must be a number 0–1")
    v = max(0.0, min(1.0, float(v)))
    vm = _num(volume_max)
    if vm is not None:
        v = min(v, max(0.0, min(1.0, float(vm))))
    return round(v, 3)


MEDIA_VOLUME_STEP = 0.1  # HA's default volume_up step


def media_volume_up_plan(current_level, volume_max, step: float = MEDIA_VOLUME_STEP) -> tuple[str, float | None]:
    """How to honour volume_max on `volume_up` (pure):
    ("up", None) no ceiling · ("set", level) one step would cross it → volume_set to the ceiling
    · ("refuse", ceiling) already at/over it, or the current level is unknown."""
    vm = _num(volume_max)
    if vm is None:
        return ("up", None)
    vm = max(0.0, min(1.0, float(vm)))
    cur = _num(current_level)
    if cur is None or float(cur) >= vm - 1e-9:
        return ("refuse", round(vm, 3))
    if float(cur) + step > vm + 1e-9:
        return ("set", round(vm, 3))
    return ("up", None)


def media_expected_attrs(action: str, data: dict) -> dict | None:
    """Attribute the ack must see for services that do not change the *state* (pure)."""
    if action == "media_player.volume_set":
        return {"volume_level": data.get("volume_level")}
    if action == "media_player.volume_mute":
        return {"is_volume_muted": data.get("is_volume_muted")}
    if action == "media_player.select_source":
        return {"source": data.get("source")}
    if action == "media_player.shuffle_set":
        return {"shuffle": data.get("shuffle")}
    if action == "media_player.repeat_set":
        return {"repeat": data.get("repeat")}
    return None


def execute_media_action(action: str, entity_id: str, payload: dict) -> dict:
    """media_player.* / music_assistant.* for one player (§15.5). Honours payload.volume_max."""
    if not entity_id:
        raise ValueError("entity_id required")
    domain, service = action.split(".", 1)
    if action == "media_player.volume_up" and payload.get("volume_max") is not None:
        current = ha(f"/states/{entity_id}")
        attrs = (
            current.get("attributes")
            if isinstance(current, dict) and isinstance(current.get("attributes"), dict)
            else {}
        )
        mode, level = media_volume_up_plan(attrs.get("volume_level"), payload.get("volume_max"))
        if mode == "refuse":
            snapshot = _snapshot_entry(current if isinstance(current, dict) else None)
            return {
                "ok": False,
                "error": "volume_max_reached",
                "entity_id": entity_id,
                "service": action,
                "volume_max": level,
                "state": snapshot.get("state"),
                "state_snapshot": {"entity_id": entity_id, **snapshot},
                "affected_entity_ids": [entity_id],
                "ha_context_id": None,
            }
        if mode == "set":
            data = {"entity_id": entity_id, "volume_level": level}
            out = call_service("media_player", "volume_set", data, expected_attrs={"volume_level": level})
            out["service"] = action
            out["clamped_to_volume_max"] = True
            return out
    if action == "music_assistant.get_queue":
        ctx, response = ha_call_service_response("music_assistant", "get_queue", {"entity_id": entity_id})
        return {
            "ok": True,
            "error": None,
            "entity_id": entity_id,
            "service": action,
            "queue": response,
            "state_snapshot": None,
            "affected_entity_ids": [entity_id],
            "ha_context_id": ctx,
        }
    service, data = build_service_data(action, entity_id, payload, HA_INFO.get("version"))
    return call_service(domain, service, data, expected_attrs=media_expected_attrs(action, data))


def execute_action(
    action: str,
    entity_id: str,
    payload: dict | None = None,
    target: dict | None = None,
) -> dict:
    """action like light.turn_on — remote/relay command path (after handle_command checks)."""
    payload = payload if isinstance(payload, dict) else {}
    if target is None and isinstance(payload.get("target"), dict):
        target = payload["target"]
    if action == "arvio.model":
        return fetch_hub_model(payload)
    if action == "arvio.list_entities":
        return {"ok": True, "entities": entities()}
    if action == "arvio.list_scenarios":
        return list_scenarios()
    if action == "arvio.list_devices":
        return list_devices()
    if action == "arvio.list_blueprints":
        return list_blueprints()
    if action == "arvio.node_red_status":
        return node_red_status()
    if action == "arvio.pairing_status":
        return pairing_status()
    if action == "arvio.zigbee_permit":
        return zigbee_permit(payload)
    if action == "arvio.upsert_scenario":
        return upsert_scenario(payload)
    if action == "arvio.delete_scenario":
        return delete_scenario(payload, entity_id)
    if action == "arvio.set_scenario_enabled":
        return set_scenario_enabled(payload, entity_id)
    if action == "arvio.trigger_scenario":
        return trigger_scenario(payload, entity_id)
    if action == "arvio.media_art":
        return media_art(payload, entity_id)
    if action == "arvio.media_browse":
        return media_browse(payload, entity_id)
    if action == "arvio.media_search":
        return media_search(payload, entity_id)
    if action == "backup.create":
        name = str(payload.get("name") or "") or None
        out = create_full_backup(name)
        # Refresh list after kickoff (job may still be running).
        tel = backup_telemetry()
        out.update({"backup_count": tel["backup_count"], "last_backup_at": tel["last_backup_at"]})
        return out
    if action == "agent.update":
        return apply_agent_update(payload)
    if "." not in action:
        raise ValueError("action must be domain.service")
    domain, _service = action.split(".", 1)
    if domain in ("media_player", "music_assistant"):
        return execute_media_action(action, entity_id, payload)

    if isinstance(target, dict) and action in TARGET_ACTIONS:
        regs = registries_for_commands()
        affected = resolve_target_entities(
            target, domain, regs["entity_regs"], regs["devices"], regs["areas"]
        )
        if entity_id and entity_id.startswith(domain + ".") and entity_id not in affected:
            affected = sorted(set(affected) | {entity_id})
        if not affected:
            raise ValueError("target matched no entities")
        service, data = build_service_data(action, "", payload, HA_INFO.get("version"))
        native = native_target_data(target, HA_INFO.get("version"))
        explicit = resolve_target_entities(
            {"entity_ids": target.get("entity_ids", target.get("entity_id"))}, domain, {}, {}, {}
        )
        if entity_id and entity_id.startswith(domain + "."):
            explicit = sorted(set(explicit) | {entity_id})
        if native:
            # HA ≥ 2024.4: native area/floor targeting; explicit ids ride along.
            data.update(native)
            if explicit:
                data["entity_id"] = explicit
        else:
            data["entity_id"] = affected
        out = call_service(domain, service, data, wait_entity_ids=affected)
        out["affected_entity_ids"] = affected
        return out

    if not entity_id:
        raise ValueError("entity_id required")
    service, data = build_service_data(action, entity_id, payload, HA_INFO.get("version"))
    return call_service(domain, service, data)


def run_batch(cmd: dict, via: str = "relay") -> dict:
    """arvio.batch {group_id, calls[]} → per-call HubCommandAck list (§6.8)."""
    cid = str(cmd.get("command_id") or "")
    payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
    group_id = str(payload.get("group_id") or cmd.get("group_id") or cid)
    calls = payload.get("calls")
    if not isinstance(calls, list) or not calls:
        raise ValueError("calls[] required")
    if len(calls) > BATCH_MAX_CALLS:
        raise ValueError(f"batch too large (max {BATCH_MAX_CALLS})")
    results = []
    affected: set = set()
    for index, call in enumerate(calls):
        call = call if isinstance(call, dict) else {}
        sub_id = str(call.get("command_id") or f"{cid}:{index}")
        sub_payload = dict(call.get("payload")) if isinstance(call.get("payload"), dict) else {}
        # The cloud puts confirm_dangerous / expires_at on the call itself (home-model.ts);
        # lift them into the same places handle_command reads for single commands.
        if call.get("confirm_dangerous") is True:
            sub_payload["confirm_dangerous"] = True
        # Calls inherit the parent's expiry (and status); a call may carry a tighter one.
        if payload.get("expires_at") and not sub_payload.get("expires_at"):
            sub_payload["expires_at"] = payload["expires_at"]
        sub = {
            "command_id": sub_id,
            "idempotency_key": str(call.get("idempotency_key") or ""),
            "action": str(call.get("action") or ""),
            "entity_id": str(call.get("entity_id") or ""),
            "target": call.get("target") if isinstance(call.get("target"), dict) else None,
            "payload": sub_payload,
            "expires_at": call.get("expires_at") or cmd.get("expires_at"),
            "status": cmd.get("status"),
            "group_id": group_id,
        }
        if sub["action"] == "arvio.batch":
            ack = make_ack(
                sub_id,
                ok=False,
                error="unknown action: nested arvio.batch",
                error_code="action_not_allowed",
            )
        elif sub["action"] in HOME_SECURITY_ACTIONS:
            # §14.3: lock / alarm actions never ride in a batch (the cloud answers 400 too).
            ack = make_ack(
                sub_id,
                ok=False,
                error="security_in_batch",
                error_code="security_in_batch",
            )
        else:
            ack = handle_command(sub, via=via)
        results.append(ack)
        affected.update(ack.get("affected_entity_ids") or [])
    all_ok = all(a.get("ok") for a in results)
    return {
        "ok": all_ok,
        "group_id": group_id,
        # HubCommandAck[] — the cloud reads `[…]` or `{results:[…]}` (acksFromBatchData).
        "results": results,
        "affected_entity_ids": sorted(affected),
        "error": None if all_ok else "partial_failure",
    }


def handle_command(cmd: dict, via: str = "relay") -> dict:
    """Relay / LAN command → HubCommandAck. Safety first (§6.9): dedupe, expiry, allowlist, confirm."""
    cmd = cmd if isinstance(cmd, dict) else {}
    cid = str(cmd.get("command_id") or "")
    idem = str(cmd.get("idempotency_key") or "")
    action = str(cmd.get("action") or "")
    entity_id = str(cmd.get("entity_id") or "")
    payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
    target = cmd.get("target") if isinstance(cmd.get("target"), dict) else None
    if target is None and isinstance(payload.get("target"), dict):
        target = payload["target"]
    # The Home app's own uuid (= its idempotency key) travels as payload.client_command_id;
    # the ack and the push `command_id` echo it so the app can match its intent.
    client_cid = str(payload.get("client_command_id") or "")
    ack_cid = client_cid or cid

    def cached_ack() -> dict | None:
        for key in (cid, idem, client_cid):
            if key:
                cached = COMMAND_ACKS.get(key)
                if isinstance(cached, dict):
                    dup = dict(cached)
                    dup["duplicate"] = True
                    return dup
        return None

    dup = cached_ack()
    if dup is not None:
        return dup

    # Same id delivered twice at once (WS + long-poll): the second waits for the first.
    keys = list(dict.fromkeys(k for k in (cid, idem, client_cid) if k))
    with COMMAND_INFLIGHT_LOCK:
        waiting = next((COMMAND_INFLIGHT[k] for k in keys if k in COMMAND_INFLIGHT), None)
        if waiting is None:
            done = threading.Event()
            for k in keys:
                COMMAND_INFLIGHT[k] = done
    if waiting is not None:
        waiting.wait(HOME_COMMAND_INFLIGHT_WAIT_S)
        dup = cached_ack()
        return dup if dup is not None else make_ack(
            ack_cid, ok=False, error="duplicate_in_flight", error_code="duplicate_in_flight"
        )

    try:
        try:
            check_command_safety(cmd, via=via)
        except CommandRejected as e:
            return make_ack(ack_cid, ok=False, error=e.error, error_code=e.code)

        try:
            if action == "arvio.batch":
                out = run_batch(cmd, via=via)
                ack = make_ack(ack_cid, out, ok=bool(out.get("ok")), error=out.get("error"))
            else:
                out = execute_action(action, entity_id, payload, target=target)
                ack = make_ack(ack_cid, out)
        except Exception as e:
            ack = make_ack(
                ack_cid,
                ok=False,
                error=str(e),
                error_code="invalid_command" if isinstance(e, ValueError) else "execution_failed",
            )

        if ack.get("ha_context_id") and ack_cid:
            CONTEXT_TO_COMMAND.put(str(ack["ha_context_id"]), ack_cid)
        for key in keys:
            COMMAND_ACKS.put(key, ack)
        return ack
    finally:
        with COMMAND_INFLIGHT_LOCK:
            for k in keys:
                COMMAND_INFLIGHT.pop(k, None)
        done.set()


# ---------------------------------------------------------------------------
# Home model, command safety and push (Agent 0.1.20)
# Contract: packages/domain/src/home-model.ts · spec docs/plans/2026-09-10-home-redesign-design.md §9.1
#
# Everything under "pure" takes plain dicts (HA states + registry payloads) and
# returns JSON-ready dicts, so the unit tests feed fixtures without a hub.
# ---------------------------------------------------------------------------

HOME_ENTITY_DOMAINS = (
    "light",
    "switch",
    "cover",
    "climate",
    "lock",
    "alarm_control_panel",
    "scene",
    "script",
    "sensor",
    "binary_sensor",
    "media_player",
)
HOME_SENSOR_DEVICE_CLASSES = ("temperature", "humidity")
# §15.2: media_player device_class speaker | tv | receiver | null (HA knows no other today).
HOME_MEDIA_DEVICE_CLASSES = ("speaker", "tv", "receiver")
HOME_BINARY_SENSOR_DEVICE_CLASSES = ("door", "window", "opening", "garage_door")
# §14.2: scripts reach the Home app by entity_id prefix only (no label lookup).
HOME_SCRIPT_PREFIX = "script.arvio_"

MODEL_LIMIT_DEFAULT = 300
# Wait-for-state after a service call (0.1.13: 1.4 s). §10.0 expects the hub ack < 3 s.
WAIT_FOR_STATE_S = 2.0
MODEL_LIMIT_MAX = 600
BATCH_MAX_CALLS = 60

# --- Μουσική (0.1.21, §15) -------------------------------------------------------------
# MediaPlayerEntityFeature bits (HA core, confirmed 2026-09-11).
MEDIA_FEATURE = {
    "PAUSE": 1,
    "SEEK": 2,
    "VOLUME_SET": 4,
    "VOLUME_MUTE": 8,
    "PREVIOUS_TRACK": 16,
    "NEXT_TRACK": 32,
    "TURN_ON": 128,
    "TURN_OFF": 256,
    "PLAY_MEDIA": 512,
    "VOLUME_STEP": 1024,
    "SELECT_SOURCE": 2048,
    "STOP": 4096,
    "PLAY": 16384,
    "SHUFFLE_SET": 32768,
    "BROWSE_MEDIA": 131072,
    "REPEAT_SET": 262144,
    "GROUPING": 524288,
    "MEDIA_ENQUEUE": 2097152,
    "SEARCH_MEDIA": 4194304,
}
MEDIA_ENQUEUE_MODES = frozenset({"add", "next", "play", "replace"})  # media_player.play_media
MA_ENQUEUE_MODES = frozenset({"play", "replace", "next", "replace_next", "add"})  # music_assistant.play_media
MEDIA_REPEAT_MODES = frozenset({"off", "all", "one"})
MEDIA_PLAYER_ACTIONS = frozenset(
    {
        "media_player.media_play",
        "media_player.media_pause",
        "media_player.media_play_pause",
        "media_player.media_stop",
        "media_player.media_next_track",
        "media_player.media_previous_track",
        "media_player.volume_set",
        "media_player.volume_mute",
        "media_player.volume_up",
        "media_player.volume_down",
        "media_player.select_source",
        "media_player.join",
        "media_player.unjoin",
        "media_player.play_media",
        "media_player.turn_on",
        "media_player.turn_off",
        "media_player.shuffle_set",
        "media_player.repeat_set",
    }
)
MUSIC_ASSISTANT_ACTIONS = frozenset(
    {"music_assistant.play_media", "music_assistant.transfer_queue", "music_assistant.get_queue"}
)
MEDIA_AGENT_ACTIONS = frozenset({"arvio.media_art", "arvio.media_browse", "arvio.media_search"})
# Music is not dangerous: every media action is allowed on the relay AND the LAN path (§15.5).
MEDIA_ACTIONS = MEDIA_PLAYER_ACTIONS | MUSIC_ASSISTANT_ACTIONS | MEDIA_AGENT_ACTIONS
# The LAN path forwards only these payload keys, and only for MEDIA_ACTIONS
# (never confirm_dangerous / code / target / expires_at).
LAN_MEDIA_PAYLOAD_KEYS = frozenset(
    {
        "volume_level",
        # No "volume_max": the ceiling belongs to the cloud (entity_meta). A caller-supplied
        # one on the unauthenticated LAN path would read like enforcement without being any.
        "is_volume_muted",
        "source",
        "group_members",
        "media_content_id",
        "media_content_type",
        "enqueue",
        "shuffle",
        "repeat",
        "media_id",
        "media_type",
        "artist",
        "album",
        "radio_mode",
        "source_player",
        "auto_play",
        "query",
        "client_command_id",
    }
)
MEDIA_ART_MAX_PX = 256
MEDIA_ART_JPEG_QUALITY = 80
MEDIA_ART_WIRE_MAX_BYTES = 40 * 1024  # hard cap for data_base64's decoded bytes
MEDIA_ART_NO_PILLOW_MAX_BYTES = 60 * 1024  # without Pillow: originals above this are never read into the cache
MEDIA_ART_FETCH_MAX_BYTES = 5 * 1024 * 1024
MEDIA_ART_CACHE_SIZE = 50
MEDIA_BROWSE_MAX_ITEMS = 200
MEDIA_SEARCH_LIMIT = 10
MEDIA_POSITION_ONLY_KEYS = frozenset({"media_position", "media_position_updated_at"})

# Services the agent executes for cloud / relay commands (spec §9.1 allowlist + §15.5 music).
AGENT_SERVICE_ALLOWLIST = frozenset(
    {
        "light.turn_on",
        "light.turn_off",
        "light.toggle",
        "switch.turn_on",
        "switch.turn_off",
        "switch.toggle",
        "cover.open_cover",
        "cover.close_cover",
        "cover.stop_cover",
        "cover.set_cover_position",
        "climate.turn_on",
        "climate.turn_off",
        "climate.set_temperature",
        "climate.set_hvac_mode",
        "climate.set_fan_mode",
        "climate.set_preset_mode",
        "climate.set_swing_mode",
        "scene.turn_on",
        "script.turn_on",
        "lock.lock",
        "lock.unlock",
        "lock.open",
        "alarm_control_panel.alarm_arm_home",
        "alarm_control_panel.alarm_arm_away",
        "alarm_control_panel.alarm_arm_night",
        "alarm_control_panel.alarm_disarm",
        # agent-level actions
        "arvio.model",
        "arvio.batch",
        "arvio.list_entities",
        "arvio.list_scenarios",
        "arvio.list_devices",
        "arvio.list_blueprints",
        "arvio.node_red_status",
        "arvio.pairing_status",
        "arvio.zigbee_permit",
        "arvio.upsert_scenario",
        "arvio.delete_scenario",
        "arvio.set_scenario_enabled",
        "arvio.trigger_scenario",
        "backup.create",
        "agent.update",
    }
    | MEDIA_ACTIONS
)

# Mirrors HOME_DANGEROUS_ACTIONS / HOME_SECURITY_ACTIONS in home-model.ts.
HOME_DANGEROUS_ACTIONS = frozenset(
    {"lock.unlock", "lock.open", "alarm_control_panel.alarm_disarm"}
)
HOME_SECURITY_ACTIONS = frozenset(
    {
        "lock.lock",
        "lock.unlock",
        "lock.open",
        "alarm_control_panel.alarm_arm_home",
        "alarm_control_panel.alarm_arm_away",
        "alarm_control_panel.alarm_arm_night",
        "alarm_control_panel.alarm_disarm",
    }
)
# Actions that accept `target {entity_ids[] | area_id | floor_id}` (§6.8).
TARGET_ACTIONS = frozenset(
    {
        "light.turn_on",
        "light.turn_off",
        "switch.turn_on",
        "switch.turn_off",
        "cover.open_cover",
        "cover.close_cover",
    }
)
# The unauthenticated LAN path (:8099, no auth) executes ONLY these; everything else in
# AGENT_SERVICE_ALLOWLIST (security actions, arvio.* writes, arvio.model, arvio.batch,
# backup.*, agent.*, any `target`) answers 403 `lan_forbidden`. `script.turn_on` is
# further limited to `script.arvio_*` (LAN_SCRIPT_PREFIX).
LAN_ALLOWED_ACTIONS = frozenset(
    {
        "light.turn_on",
        "light.turn_off",
        "switch.turn_on",
        "switch.turn_off",
        "cover.open_cover",
        "cover.close_cover",
        "cover.stop_cover",
        "cover.set_cover_position",
        "climate.set_temperature",
        "climate.set_hvac_mode",
        "climate.set_fan_mode",
        "climate.set_preset_mode",
        "climate.set_swing_mode",
        "climate.turn_on",
        "climate.turn_off",
        "scene.turn_on",
        "script.turn_on",
        # read-only
        "arvio.list_entities",
        "arvio.pairing_status",
    }
    # §15.5: transport/volume/grouping is not dangerous. Library, playlists and
    # album art (arvio.media_*) stay cloud-only: they are personal data and the
    # LAN path is unauthenticated until the HA-ingress hardening lands.
    | MEDIA_PLAYER_ACTIONS
    | MUSIC_ASSISTANT_ACTIONS
)
LAN_SCRIPT_PREFIX = "script.arvio_"

HA_FLOORS_MIN_VERSION = (2024, 4)
HA_KELVIN_MIN_VERSION = (2022, 12)

COMMAND_LRU_SIZE = 500
STATE_COALESCE_S = 1.0
HEARTBEAT_INTERVAL_S = 30.0
REGISTRY_REFRESH_S = 600.0
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0


class Backoff:
    """Reconnect delay: exponential 1 → 60 s plus up to 50 % jitter; `reset()` after success.

    Shared by the HA events websocket and the relay loop.
    """

    def __init__(self, base: float = BACKOFF_MIN_S, cap: float = BACKOFF_MAX_S, rand=random.uniform) -> None:
        self.base = float(base)
        self.cap = float(cap)
        self._rand = rand
        self.delay = self.base

    def next_sleep(self) -> float:
        """Seconds to sleep before the next attempt; doubles the delay for the one after."""
        delay = self.delay
        self.delay = min(self.cap, delay * 2)
        return delay + self._rand(0, delay / 2)

    def reset(self) -> None:
        self.delay = self.base


class CommandRejected(Exception):
    """Raised by the safety checks; `code` is the machine-readable ack `error_code`,
    `error` the ack `error` string (defaults to the code)."""

    def __init__(self, code: str, detail: str = "", error: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.error = error or code


class LRU:
    """Tiny thread-safe LRU map (command acks, HA context → command id)."""

    def __init__(self, capacity: int = COMMAND_LRU_SIZE) -> None:
        self.capacity = max(1, int(capacity))
        self._d: "OrderedDict[str, object]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            if key not in self._d:
                return None
            self._d.move_to_end(key)
            return self._d[key]

    def put(self, key: str, value) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.capacity:
                self._d.popitem(last=False)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._d

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)


COMMAND_ACKS = LRU(COMMAND_LRU_SIZE)  # command_id / idempotency_key → ack
CONTEXT_TO_COMMAND = LRU(COMMAND_LRU_SIZE)  # HA context id → command_id
COMMAND_INFLIGHT: dict = {}  # command_id / idempotency_key → Event while executing
COMMAND_INFLIGHT_LOCK = threading.Lock()
HOME_COMMAND_INFLIGHT_WAIT_S = 50.0

HA_INFO: dict = {"version": None, "time_zone": None}

# Wait-for-state feed: `state_changed` events land here (handle_ha_event) so a command
# waiting on a group target does not poll the full /states dump every 150 ms.
STATE_FEED = threading.Condition()
STATE_FEED_RECENT = LRU(1000)  # entity_id → (received_at, state object)
STATE_FEED_LIVE = False  # True while the HA events websocket is subscribed
WAIT_FOR_STATE_REST_MAX_ENTITIES = 8


def note_state_change(entity_id: str, new_state: dict) -> None:
    """Record a `state_changed` for entities a command may be waiting on; wakes the waiters."""
    if not entity_id or not isinstance(new_state, dict):
        return
    with STATE_FEED:
        STATE_FEED_RECENT.put(entity_id, (time.time(), new_state))
        STATE_FEED.notify_all()


def _read_states_rest(entity_ids: list) -> dict:
    """Per-entity GET /states/{id} for small groups; one /states dump only for large ones."""
    if not entity_ids:
        return {}
    if len(entity_ids) <= WAIT_FOR_STATE_REST_MAX_ENTITIES:
        out: dict = {}
        for eid in entity_ids:
            st = ha(f"/states/{eid}")
            if isinstance(st, dict):
                out[eid] = st
        return out
    wanted = set(entity_ids)
    all_states = ha("/states")
    return {
        str(s.get("entity_id")): s
        for s in (all_states if isinstance(all_states, list) else [])
        if isinstance(s, dict) and str(s.get("entity_id")) in wanted
    }


def _overlay_feed(states: dict, entity_ids: list, since: float) -> dict:
    """Newer `state_changed` objects (received after `since`) win over the REST read."""
    out = dict(states)
    for eid in entity_ids:
        hit = STATE_FEED_RECENT.get(eid)
        if isinstance(hit, tuple) and hit[0] >= since and isinstance(hit[1], dict):
            out[eid] = hit[1]
    return out


def wait_for_states(
    domain: str,
    expected: str | None,
    entity_ids: list,
    sent_at: float | None = None,
    expected_attrs: dict | None = None,
) -> dict:
    """Wait ≤ WAIT_FOR_STATE_S for every entity to reach `expected` (state and/or attributes);
    returns {entity_id: state}.

    With the HA event feed live the wait is event-driven (one REST read, then condition
    waits); otherwise it polls REST every 150 ms — per entity when ≤ 8 affected.
    Zigbee/Wi‑Fi devices often lag; don't report stale pre-command state.
    """
    since = time.time() if sent_at is None else sent_at
    if not entity_ids:
        return {}
    states = _overlay_feed(_read_states_rest(entity_ids), entity_ids, since)

    def settled(current: dict) -> bool:
        return all(
            _state_matches(domain, expected, _state_of(current.get(e))) and _attrs_match(current.get(e), expected_attrs)
            for e in entity_ids
        )

    if (expected is None and not expected_attrs) or settled(states):
        return states
    deadline = time.time() + WAIT_FOR_STATE_S
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        if STATE_FEED_LIVE:
            with STATE_FEED:
                STATE_FEED.wait(min(remaining, 0.5))
            states = _overlay_feed(states, entity_ids, since)
        else:
            time.sleep(min(remaining, 0.15))
            states = _overlay_feed(_read_states_rest(entity_ids), entity_ids, since)
        if settled(states):
            return states
    if STATE_FEED_LIVE:
        # Not confirmed by events within the wait: report what HA sees right now.
        states = _overlay_feed(_read_states_rest(entity_ids), entity_ids, since)
    return states


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_iso_ts(value) -> float | None:
    """ISO-8601 (with Z or offset) → epoch seconds; None when unparsable."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_ha_version(version) -> tuple[int, int] | None:
    """'2025.8.1' → (2025, 8); None when unknown."""
    if not version:
        return None
    parts = str(version).split(".")
    try:
        return int(parts[0]), (int(parts[1]) if len(parts) > 1 else 0)
    except (TypeError, ValueError):
        return None


def ha_version_at_least(version, minimum: tuple[int, int]) -> bool:
    parsed = parse_ha_version(version)
    if parsed is None:
        # Unknown version: assume a current HA (the add-on ships against current HAOS).
        return True
    return parsed >= minimum


def mireds_to_kelvin(mireds) -> int | None:
    try:
        m = float(mireds)
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None
    return int(round(1_000_000 / m))


def kelvin_to_mireds(kelvin) -> int | None:
    try:
        k = float(kelvin)
    except (TypeError, ValueError):
        return None
    if k <= 0:
        return None
    return int(round(1_000_000 / k))


# --- pure: registry normalisation --------------------------------------------


def normalize_floors(raw) -> list:
    """config/floor_registry/list → [{floor_id, name, level, icon, aliases}] (HA ≥ 2024.4)."""
    out = []
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict) or not f.get("floor_id"):
            continue
        level = f.get("level")
        try:
            level = int(level) if level is not None else None
        except (TypeError, ValueError):
            level = None
        out.append(
            {
                "floor_id": str(f["floor_id"]),
                "name": str(f.get("name") or f["floor_id"]),
                "level": level,
                "icon": f.get("icon"),
                "aliases": _str_list(f.get("aliases")),
            }
        )
    out.sort(key=lambda x: (x["level"] is None, x["level"] or 0, x["name"]))
    return out


def normalize_areas(raw) -> dict:
    """config/area_registry/list → {area_id: {area_id, name, floor_id, icon, picture, …}}."""
    out: dict = {}
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict) or not a.get("area_id"):
            continue
        aid = str(a["area_id"])
        out[aid] = {
            "area_id": aid,
            "name": str(a.get("name") or aid),
            "floor_id": a.get("floor_id") or None,
            "icon": a.get("icon"),
            "picture": a.get("picture"),
            "aliases": _str_list(a.get("aliases")),
            # HA ≥ 2024.11 area registry fields; absent on older cores.
            "temperature_entity_id": a.get("temperature_entity_id"),
            "humidity_entity_id": a.get("humidity_entity_id"),
        }
    return out


def normalize_devices(raw) -> dict:
    """config/device_registry/list → {device_id: {device_id, name, area_id}}."""
    out: dict = {}
    for d in raw if isinstance(raw, list) else []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        did = str(d["id"])
        name = d.get("name_by_user") or d.get("name")
        out[did] = {
            "device_id": did,
            "name": str(name) if name else None,
            "area_id": d.get("area_id") or None,
        }
    return out


def normalize_entity_registry(raw) -> dict:
    """Accept either registry shape → {entity_id: {device_id, area_id, entity_category, icon, labels, hidden, name}}.

    - config/entity_registry/list_for_display: {"entity_categories": {"0": "config", "1": "diagnostic"},
      "entities": [{"ei", "di", "ai", "ec", "ic", "lb", "hb", "en", …}]}
    - config/entity_registry/list: [{"entity_id", "device_id", "area_id", "entity_category", "icon",
      "labels", "hidden_by", "disabled_by", …}]
    """
    categories = {"0": "config", "1": "diagnostic"}
    items: list = []
    if isinstance(raw, dict):
        ec_map = raw.get("entity_categories")
        if isinstance(ec_map, dict) and ec_map:
            categories = {str(k): str(v) for k, v in ec_map.items()}
        items = raw.get("entities") if isinstance(raw.get("entities"), list) else []
    elif isinstance(raw, list):
        items = raw
    out: dict = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        eid = it.get("ei") or it.get("entity_id")
        if not eid:
            continue
        if it.get("disabled_by"):
            continue
        category = None
        if "ec" in it and it.get("ec") is not None:
            category = categories.get(str(it["ec"]))
        elif it.get("entity_category") in ("config", "diagnostic"):
            category = str(it["entity_category"])
        labels = it.get("lb") if "lb" in it else it.get("labels")
        if not isinstance(labels, list):
            labels = []
        out[str(eid)] = {
            "entity_id": str(eid),
            "device_id": it.get("di") if "di" in it else it.get("device_id"),
            "area_id": it.get("ai") if "ai" in it else it.get("area_id"),
            "entity_category": category if category in ("config", "diagnostic") else None,
            "icon": it.get("ic") if "ic" in it else it.get("icon"),
            "labels": [str(x) for x in labels],
            "hidden": bool(it.get("hb") or it.get("hidden_by")),
            "name": it.get("name") or it.get("en") or it.get("original_name"),
            "original_device_class": it.get("original_device_class"),
            # integration domain ("music_assistant", "cast", "sonos", …) — §15.2
            "platform": it.get("pl") if "pl" in it else it.get("platform"),
        }
    return out


def registry_fingerprint(floors: list, areas: dict, devices: dict, entity_regs: dict) -> str:
    """Stable hash of what the Home model depends on; a change bumps snapshot_version."""
    material = {
        "floors": [(f["floor_id"], f["name"], f["level"], f.get("icon")) for f in floors],
        "areas": sorted(
            (a["area_id"], a["name"], a["floor_id"], a.get("icon"), a.get("picture"))
            for a in areas.values()
        ),
        "devices": sorted((d["device_id"], d["name"], d["area_id"]) for d in devices.values()),
        "entities": sorted(
            (
                r["entity_id"],
                r.get("device_id"),
                r.get("area_id"),
                r.get("entity_category"),
                r.get("icon"),
                tuple(sorted(r.get("labels") or [])),
                r.get("hidden"),
                r.get("platform"),
            )
            for r in entity_regs.values()
        ),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --- pure: entities ----------------------------------------------------------


def effective_area_id(reg: dict | None, devices: dict) -> str | None:
    """entity.area_id ?? device.area_id ?? None."""
    if not reg:
        return None
    if reg.get("area_id"):
        return str(reg["area_id"])
    did = reg.get("device_id")
    if did and did in devices and devices[did].get("area_id"):
        return str(devices[did]["area_id"])
    return None


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _str_list(value) -> list:
    return [str(x) for x in value] if isinstance(value, list) else []


def _int_or_none(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def media_player_exposed(device_class) -> bool:
    """§15.2: speaker | tv | receiver | null reach the Home app."""
    return device_class in (None, "") or str(device_class) in HOME_MEDIA_DEVICE_CLASSES


def media_art_hash(media_content_id, entity_picture) -> str | None:
    """sha1(media_content_id + entity_picture); None without a picture. The app refetches art on change."""
    if not entity_picture:
        return None
    return hashlib.sha1((str(media_content_id or "") + str(entity_picture)).encode("utf-8")).hexdigest()


def entity_capabilities(domain: str, attrs: dict) -> dict:
    color_modes = attrs.get("supported_color_modes") or []
    if not isinstance(color_modes, list):
        color_modes = []
    if domain == "media_player":
        sf = _int_or_none(attrs.get("supported_features")) or 0
        return {
            "volume": bool(sf & MEDIA_FEATURE["VOLUME_SET"]),
            "volume_step": bool(sf & MEDIA_FEATURE["VOLUME_STEP"]),
            "mute": bool(sf & MEDIA_FEATURE["VOLUME_MUTE"]),
            "next_previous": bool(sf & (MEDIA_FEATURE["NEXT_TRACK"] | MEDIA_FEATURE["PREVIOUS_TRACK"])),
            "play_media": bool(sf & MEDIA_FEATURE["PLAY_MEDIA"]),
            "source": bool(sf & MEDIA_FEATURE["SELECT_SOURCE"]),
            "browse": bool(sf & MEDIA_FEATURE["BROWSE_MEDIA"]),
            "search": bool(sf & MEDIA_FEATURE["SEARCH_MEDIA"]),
            "grouping": bool(sf & MEDIA_FEATURE["GROUPING"]),
            "shuffle": bool(sf & MEDIA_FEATURE["SHUFFLE_SET"]),
            "repeat": bool(sf & MEDIA_FEATURE["REPEAT_SET"]),
            "power": bool(sf & (MEDIA_FEATURE["TURN_ON"] | MEDIA_FEATURE["TURN_OFF"])),
        }
    return {
        "brightness": domain == "light"
        and (
            "brightness" in color_modes
            or attrs.get("brightness") is not None
            or attrs.get("brightness_pct") is not None
        ),
        "color": domain == "light"
        and any(m in color_modes for m in ("rgb", "rgbw", "rgbww", "hs", "xy")),
        "color_temp": domain == "light"
        and (
            "color_temp" in color_modes
            or attrs.get("color_temp") is not None
            or attrs.get("color_temp_kelvin") is not None
        ),
        "position": domain == "cover" and attrs.get("current_position") is not None,
        "temperature": domain == "climate",
        "preset": domain == "climate" and bool(attrs.get("preset_modes")),
        "swing": domain == "climate" and bool(attrs.get("swing_modes")),
        "fan": domain == "climate" and bool(attrs.get("fan_modes")),
    }


def typed_attrs(
    domain: str,
    state: str | None,
    attrs: dict,
    labels: list | None = None,
    platform: str | None = None,
) -> dict:
    """EntityAttrs subset (home-model.ts). Only what the Home app renders.

    media_player (0.1.21, §15.2): the media fields are not yet in home-model.ts — the cloud
    stream adds them; strict readers ignore them meanwhile."""
    out: dict = {}
    if domain == "light":
        brightness = attrs.get("brightness")
        if attrs.get("brightness_pct") is not None:
            out["brightness_pct"] = int(round(float(attrs["brightness_pct"])))
        elif _num(brightness) is not None:
            out["brightness_pct"] = int(round(float(brightness) / 255 * 100))
        else:
            out["brightness_pct"] = None
        rgb = attrs.get("rgb_color")
        out["rgb_color"] = [int(x) for x in rgb] if isinstance(rgb, list) else None
        if attrs.get("color_temp_kelvin") is not None:
            out["color_temp_kelvin"] = _num(attrs.get("color_temp_kelvin"))
        else:
            # HA < 2022.12 exposes mireds only.
            out["color_temp_kelvin"] = mireds_to_kelvin(attrs.get("color_temp"))
        if attrs.get("min_color_temp_kelvin") is not None:
            out["min_color_temp_kelvin"] = _num(attrs.get("min_color_temp_kelvin"))
        else:
            out["min_color_temp_kelvin"] = mireds_to_kelvin(attrs.get("max_mireds"))
        if attrs.get("max_color_temp_kelvin") is not None:
            out["max_color_temp_kelvin"] = _num(attrs.get("max_color_temp_kelvin"))
        else:
            out["max_color_temp_kelvin"] = mireds_to_kelvin(attrs.get("min_mireds"))
        out["supported_color_modes"] = _str_list(attrs.get("supported_color_modes"))
    elif domain == "climate":
        out["current_temperature"] = _num(attrs.get("current_temperature"))
        out["temperature"] = _num(attrs.get("temperature"))
        out["hvac_mode"] = state if state not in (None, "unavailable", "unknown") else None
        out["hvac_modes"] = _str_list(attrs.get("hvac_modes"))
        out["fan_mode"] = attrs.get("fan_mode")
        out["fan_modes"] = _str_list(attrs.get("fan_modes"))
        out["preset_mode"] = attrs.get("preset_mode")
        out["preset_modes"] = _str_list(attrs.get("preset_modes"))
        out["swing_mode"] = attrs.get("swing_mode")
        out["swing_modes"] = _str_list(attrs.get("swing_modes"))
        out["min_temp"] = _num(attrs.get("min_temp"))
        out["max_temp"] = _num(attrs.get("max_temp"))
        action = attrs.get("hvac_action")
        out["hvac_action"] = (
            str(action) if action in ("heating", "cooling", "idle", "off", "drying", "fan") else None
        )
    elif domain == "cover":
        out["current_position"] = _num(attrs.get("current_position"))
    elif domain == "lock":
        # §14.2: jammed / opening / open are lock *states*; no is_jammed attribute.
        out["changed_by"] = attrs.get("changed_by")
    elif domain == "alarm_control_panel":
        car = attrs.get("code_arm_required")
        out["code_arm_required"] = bool(car) if car is not None else None
        out["code_format"] = attrs.get("code_format")
        # §14.2: absent by default; only emitted when the panel reports them.
        for key in ("arming_time", "delay_time"):
            if attrs.get(key) is not None:
                out[key] = _num(attrs.get(key))
    elif domain == "scene":
        # Scene entities list their members in the `entity_id` attribute (best-effort).
        out["scene_entity_ids"] = _str_list(attrs.get("entity_id"))
    elif domain == "script":
        out["labels"] = list(labels or [])
    elif domain == "sensor":
        out["value"] = None if state in (None, "unavailable", "unknown") else (_num(state) if _num(state) is not None else state)
        out["unit_of_measurement"] = attrs.get("unit_of_measurement")
    elif domain == "binary_sensor":
        out["value"] = None if state in (None, "unavailable", "unknown") else state
        out["unit_of_measurement"] = None
    elif domain == "media_player":
        for key in (
            "media_title",
            "media_artist",
            "media_album_name",
            "app_name",
            "media_content_id",
            "media_content_type",
            "source",
        ):
            value = attrs.get(key)
            out[key] = str(value) if value not in (None, "") else None
        out["media_duration"] = _num(attrs.get("media_duration"))
        out["media_position"] = _num(attrs.get("media_position"))
        updated = attrs.get("media_position_updated_at")
        out["media_position_updated_at"] = str(updated) if updated else None
        out["volume_level"] = _num(attrs.get("volume_level"))
        muted = attrs.get("is_volume_muted")
        out["is_volume_muted"] = bool(muted) if muted is not None else None
        out["source_list"] = _str_list(attrs.get("source_list"))
        out["group_members"] = _str_list(attrs.get("group_members"))
        shuffle = attrs.get("shuffle")
        out["shuffle"] = bool(shuffle) if shuffle is not None else None
        repeat = attrs.get("repeat")
        out["repeat"] = str(repeat) if repeat else None
        out["supported_features"] = _int_or_none(attrs.get("supported_features"))
        out["platform"] = str(platform) if platform else None
        out["art_hash"] = media_art_hash(attrs.get("media_content_id"), attrs.get("entity_picture"))
    return out


def entity_model_from_state(
    state_obj: dict, reg: dict | None, devices: dict, areas: dict
) -> dict | None:
    """One HA state (+ registry entry) → EntityModel (without hub_id), or None when not exposed."""
    if not isinstance(state_obj, dict):
        return None
    eid = str(state_obj.get("entity_id") or "")
    if "." not in eid:
        return None
    domain = eid.split(".", 1)[0]
    if domain not in HOME_ENTITY_DOMAINS:
        return None
    attrs = state_obj.get("attributes") if isinstance(state_obj.get("attributes"), dict) else {}
    reg = reg or {}
    if reg.get("entity_category") in ("config", "diagnostic"):
        return None
    if reg.get("hidden"):
        # §14.2: list_for_display `hb` (hidden) entries never reach the Home app.
        return None
    device_class = attrs.get("device_class") or reg.get("original_device_class")
    device_class = str(device_class) if device_class else None
    if domain == "sensor" and device_class not in HOME_SENSOR_DEVICE_CLASSES:
        return None
    if domain == "binary_sensor" and device_class not in HOME_BINARY_SENSOR_DEVICE_CLASSES:
        return None
    if domain == "media_player" and not media_player_exposed(device_class):
        return None
    labels = [str(x) for x in (reg.get("labels") or [])]
    if domain == "script" and not eid.startswith(HOME_SCRIPT_PREFIX):
        return None
    state = state_obj.get("state")
    state = str(state) if state is not None else None
    area_id = effective_area_id(reg, devices)
    floor_id = areas[area_id]["floor_id"] if area_id and area_id in areas else None
    device_id = reg.get("device_id") or None
    supported_features = attrs.get("supported_features")
    try:
        supported_features = int(supported_features) if supported_features is not None else None
    except (TypeError, ValueError):
        supported_features = None
    return {
        "entity_id": eid,
        "domain": domain,
        "name": str(attrs.get("friendly_name") or reg.get("name") or eid),
        "state": state,
        "last_changed": state_obj.get("last_changed"),
        "device_id": device_id,
        "area_id": area_id,
        "floor_id": floor_id,
        "icon": reg.get("icon") or attrs.get("icon"),
        "device_class": device_class,
        "entity_category": None,
        # Not in EntityModel (ignored by strict readers); §14.3 alarm buttons need it.
        "supported_features": supported_features,
        "capabilities": entity_capabilities(domain, attrs),
        "attrs": typed_attrs(domain, state, attrs, labels, platform=reg.get("platform")),
    }


# --- pure: scenarios ---------------------------------------------------------


def parse_show_in_home(description) -> bool:
    """Partner marks a scenario for the Home app in the automation description:
    {"arvio": {"show_in_home": true}} (whole description or embedded object). Default false."""
    if not isinstance(description, str) or "{" not in description:
        return False
    start, end = description.find("{"), description.rfind("}")
    candidates = [description.strip()]
    if 0 <= start < end:
        candidates.append(description[start : end + 1])
    for text in candidates:
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        arv = obj.get("arvio")
        if isinstance(arv, dict):
            return bool(arv.get("show_in_home"))
        if "show_in_home" in obj:
            return bool(obj.get("show_in_home"))
    return False


def scenario_show_in_home(cfg: dict | None) -> bool:
    if not isinstance(cfg, dict):
        return False
    if parse_show_in_home(cfg.get("description")):
        return True
    variables = cfg.get("variables")
    if isinstance(variables, dict) and "arvio_show_in_home" in variables:
        return bool(variables.get("arvio_show_in_home"))
    return False


def collect_action_entity_ids(node, out: set | None = None) -> list:
    """Every entity_id referenced anywhere in an automation's action tree (best-effort)."""
    if out is None:
        out = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, str) and "." in item and item not in ("all", "none"):
                        out.add(item)
            else:
                collect_action_entity_ids(value, out)
    elif isinstance(node, list):
        for item in node:
            collect_action_entity_ids(item, out)
    return sorted(out)


def build_scenarios(states: list, automation_configs: dict) -> list:
    out = []
    for e in states:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entity_id") or "")
        if not eid.startswith("automation."):
            continue
        attrs = e.get("attributes") if isinstance(e.get("attributes"), dict) else {}
        sid = str(attrs.get("id") or "")
        if not _is_arvio_scenario_id(sid):
            continue
        cfg = automation_configs.get(sid) if isinstance(automation_configs, dict) else None
        cfg = cfg if isinstance(cfg, dict) else {}
        actions = cfg.get("actions") if "actions" in cfg else cfg.get("action")
        out.append(
            {
                "scenario_id": sid,
                "name": str(cfg.get("alias") or attrs.get("friendly_name") or sid),
                "show_in_home": scenario_show_in_home(cfg),
                "action_entity_ids": collect_action_entity_ids(actions),
                "entity_id": eid,
                "enabled": str(e.get("state") or "") != "off",
            }
        )
    out.sort(key=lambda s: s["scenario_id"])
    return out


# --- pure: model -------------------------------------------------------------


def clamp_paging(offset, limit) -> tuple[int, int]:
    try:
        off = max(0, int(offset or 0))
    except (TypeError, ValueError):
        off = 0
    try:
        lim = int(limit) if limit is not None else MODEL_LIMIT_DEFAULT
    except (TypeError, ValueError):
        lim = MODEL_LIMIT_DEFAULT
    lim = max(1, min(MODEL_LIMIT_MAX, lim))
    return off, lim


def build_hub_model(
    states: list,
    floors_raw,
    areas_raw,
    devices_raw,
    entity_registry_raw,
    automation_configs: dict | None = None,
    *,
    ha_version=None,
    timezone_name=None,
    snapshot_version: int = 1,
    offset=0,
    limit=MODEL_LIMIT_DEFAULT,
    ma_config_entry_id=None,
) -> dict:
    """HubModelPayload (home-model.ts) from raw HA data. Pure.

    `ma_available` / `ma_config_entry_id` (0.1.21, §15.2) are not in home-model.ts yet."""
    floors = normalize_floors(floors_raw)
    areas = normalize_areas(areas_raw)
    devices = normalize_devices(devices_raw)
    regs = normalize_entity_registry(entity_registry_raw)
    states = states if isinstance(states, list) else []
    # An area may only point at a floor the registry knows (none at all on HA < 2024.4).
    known_floors = {f["floor_id"] for f in floors}
    for area in areas.values():
        if area["floor_id"] not in known_floors:
            area["floor_id"] = None

    entities = []
    sun = None
    for st in states:
        if not isinstance(st, dict):
            continue
        eid = str(st.get("entity_id") or "")
        if eid == "sun.sun":
            attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
            sun = {
                "next_rising": attrs.get("next_rising"),
                "next_setting": attrs.get("next_setting"),
            }
            continue
        model = entity_model_from_state(st, regs.get(eid), devices, areas)
        if model is not None:
            entities.append(model)
    entities.sort(key=lambda x: x["entity_id"])
    off, lim = clamp_paging(offset, limit)
    total = len(entities)

    return {
        "snapshot_version": int(snapshot_version),
        "agent_version": AGENT_VERSION,
        "ha_version": str(ha_version) if ha_version else None,
        "timezone": str(timezone_name) if timezone_name else None,
        "sun": sun,
        "floors": floors,
        "areas": sorted(areas.values(), key=lambda a: a["name"]),
        "devices": sorted(devices.values(), key=lambda d: d["device_id"]),
        "entities": entities[off : off + lim],
        "scenarios": build_scenarios(states, automation_configs or {}),
        "ma_available": bool(ma_config_entry_id),
        "ma_config_entry_id": str(ma_config_entry_id) if ma_config_entry_id else None,
        "offset": off,
        "limit": lim,
        "total": total,
    }


def resolve_target_entities(target: dict | None, domain: str, entity_regs: dict, devices: dict, areas: dict) -> list:
    """`target {entity_ids[] | entity_id | area_id | floor_id}` → sorted entity ids of `domain`.

    Area/floor expansion mirrors HA's own target resolution: entity area ?? device area,
    config/diagnostic and hidden entities excluded.
    """
    if not isinstance(target, dict):
        return []
    found: set = set()
    ids = target.get("entity_ids")
    if ids is None and target.get("entity_id") is not None:
        ids = target.get("entity_id")
    for item in ids if isinstance(ids, list) else ([ids] if isinstance(ids, str) else []):
        if isinstance(item, str) and item.startswith(domain + "."):
            found.add(item)
    area_ids: set = set()
    if target.get("area_id"):
        area_ids.add(str(target["area_id"]))
    if target.get("floor_id"):
        fid = str(target["floor_id"])
        area_ids |= {aid for aid, a in areas.items() if a.get("floor_id") == fid}
    if area_ids:
        for eid, reg in entity_regs.items():
            if not eid.startswith(domain + "."):
                continue
            if reg.get("entity_category") or reg.get("hidden"):
                continue
            if effective_area_id(reg, devices) in area_ids:
                found.add(eid)
    return sorted(found)


def native_target_data(target: dict, ha_version) -> dict | None:
    """Service-data fragment for HA-native area/floor targeting, or None when not applicable."""
    if not isinstance(target, dict):
        return None
    if not ha_version_at_least(ha_version, HA_FLOORS_MIN_VERSION):
        return None
    data: dict = {}
    if target.get("area_id"):
        data["area_id"] = str(target["area_id"])
    if target.get("floor_id"):
        data["floor_id"] = str(target["floor_id"])
    return data or None


# --- pure: command safety (§6.9 / §9.4) ---------------------------------------


def check_command_safety(cmd: dict, via: str = "relay", now: float | None = None) -> None:
    """Raise CommandRejected for allowlist / expiry / dangerous-confirm / LAN violations."""
    now = time.time() if now is None else now
    action = str(cmd.get("action") or "")
    payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
    target = cmd.get("target") if isinstance(cmd.get("target"), dict) else payload.get("target")
    if action not in AGENT_SERVICE_ALLOWLIST:
        # "unknown action" in the message makes the cloud fall back / surface it (§14.3).
        raise CommandRejected(
            "action_not_allowed", f"action not allowed: {action}", error=f"unknown action: {action}"
        )
    if via == "lan":
        # Explicit allowlist (not a denylist): anything not listed is forbidden locally.
        if action not in LAN_ALLOWED_ACTIONS:
            raise CommandRejected("lan_forbidden", f"{action} not available on the LAN path")
        if action == "script.turn_on" and not str(cmd.get("entity_id") or "").startswith(LAN_SCRIPT_PREFIX):
            raise CommandRejected("lan_forbidden", f"only {LAN_SCRIPT_PREFIX}* scripts on the LAN path")
        if isinstance(target, dict):
            raise CommandRejected("lan_forbidden", "target not available on the LAN path")
    status = str(cmd.get("status") or "")
    if status == "expired":
        raise CommandRejected("expired", "command already expired")
    if status == "cancelled":
        raise CommandRejected("cancelled", "command cancelled")
    # The relay stamps `expires_at` on the command; the cloud also puts its own
    # (tighter, TTL-derived) copy in payload.expires_at — the earliest one wins.
    for candidate in (cmd.get("expires_at"), payload.get("expires_at")):
        if candidate in (None, ""):
            continue
        exp_ts = parse_iso_ts(candidate)
        if exp_ts is None:
            # A deadline we cannot read is not a deadline we may ignore.
            raise CommandRejected("expired", "unparsable expires_at")
        if now > exp_ts:
            raise CommandRejected("expired", "command expired")
    if action in HOME_DANGEROUS_ACTIONS and payload.get("confirm_dangerous") is not True:
        raise CommandRejected("confirm_required", "dangerous action needs confirm_dangerous")
    if target is not None and action not in TARGET_ACTIONS and action != "arvio.batch":
        raise CommandRejected("target_not_supported", f"target not supported for {action}")


def make_ack(
    command_id: str,
    out: dict | None = None,
    *,
    ok: bool = True,
    error: str | None = None,
    error_code: str | None = None,
) -> dict:
    """HubCommandAck (home-model.ts) + legacy call_service fields for older cloud readers."""
    out = out if isinstance(out, dict) else {}
    ack = dict(out)
    ack.update(
        {
            "command_id": command_id,
            "ok": bool(ok) and bool(out.get("ok", True)),
            "state_snapshot": out.get("state_snapshot"),
            "affected_entity_ids": list(out.get("affected_entity_ids") or []),
            "ha_context_id": out.get("ha_context_id"),
            "hub_ts": now_iso(),
            "error": error if error is not None else out.get("error"),
        }
    )
    if not ack["ok"] and not error_code:
        # Every rejection carries a machine-readable code (contract `error_code`).
        error_code = ack.get("error_code") or default_error_code(ack.get("error"))
    if error_code:
        ack["error_code"] = error_code
    return ack


# Errors the executor already reports as stable strings → same string as the code.
KNOWN_ERROR_CODES = frozenset(
    {"state_not_confirmed", "partial_failure", "duplicate_in_flight", "security_in_batch", "volume_max_reached"}
)


def default_error_code(error) -> str:
    err_s = str(error or "")
    if err_s in KNOWN_ERROR_CODES:
        return err_s
    return "execution_failed"


# --- IO: HA reads ------------------------------------------------------------


def fetch_registries() -> dict:
    """Floors, areas, devices, entity registry via one HA websocket connection.

    Floors are optional (HA < 2024.4 → []); the other three raise when HA is unreachable.
    """
    with HaWs(timeout=20.0) as ws:
        try:
            # TODO confirm: config/floor_registry/list is frontend-internal but stable (HA ≥ 2024.4).
            floors = ws.command("config/floor_registry/list")
        except Exception:
            floors = []
        # TODO confirm: config/area_registry/list is frontend-internal but stable.
        areas = ws.command("config/area_registry/list")
        # TODO confirm: config/device_registry/list is frontend-internal but stable.
        devices = ws.command("config/device_registry/list")
        try:
            # TODO confirm: config/entity_registry/list_for_display is frontend-internal but stable (HA ≥ 2023.3).
            entity_registry = ws.command("config/entity_registry/list_for_display")
        except Exception:
            # TODO confirm: config/entity_registry/list is frontend-internal but stable.
            entity_registry = ws.command("config/entity_registry/list")
    return {
        "floors": floors,
        "areas": areas,
        "devices": devices,
        "entity_registry": entity_registry,
    }


def fetch_arvio_automation_configs(states: list) -> dict:
    out: dict = {}
    for e in states if isinstance(states, list) else []:
        if not isinstance(e, dict):
            continue
        if not str(e.get("entity_id") or "").startswith("automation."):
            continue
        attrs = e.get("attributes") if isinstance(e.get("attributes"), dict) else {}
        sid = str(attrs.get("id") or "")
        if not _is_arvio_scenario_id(sid):
            continue
        cfg = ha(f"/config/automation/config/{sid}", timeout=15)
        out[sid] = cfg if isinstance(cfg, dict) else {}
    return out


SNAPSHOT_LOCK = threading.Lock()


def get_snapshot_version() -> int:
    try:
        return max(1, int(load_hub().get("snapshot_version") or 1))
    except (TypeError, ValueError):
        return 1


def bump_snapshot_version(reason: str = "") -> int:
    with SNAPSHOT_LOCK:
        st = load_hub()
        try:
            v = int(st.get("snapshot_version") or 1) + 1
        except (TypeError, ValueError):
            v = 2
        st["snapshot_version"] = v
        st["snapshot_bumped_at"] = now_iso()
        if reason:
            st["snapshot_reason"] = reason
        save_hub(st)
        return v


def ensure_snapshot_for_fingerprint(fingerprint: str, force: bool = False) -> tuple[int, bool]:
    """Persist the registry fingerprint; bump snapshot_version when it changed (or `force`).

    → (version, bumped). `force` is used after HA registry-updated events (§14.3): the
    event itself is the signal, even when nothing the Home model reads has changed.
    """
    with SNAPSHOT_LOCK:
        st = load_hub()
        try:
            v = max(1, int(st.get("snapshot_version") or 1))
        except (TypeError, ValueError):
            v = 1
        bumped = False
        changed = st.get("registry_fingerprint") != fingerprint
        if changed or force:
            if force or st.get("registry_fingerprint") is not None:
                v += 1
                bumped = True
            st["registry_fingerprint"] = fingerprint
            st["snapshot_version"] = v
            st["snapshot_bumped_at"] = now_iso()
            save_hub(st)
        elif st.get("snapshot_version") != v:
            st["snapshot_version"] = v
            save_hub(st)
        return v, bumped


class RegistrySnapshot:
    """Immutable view of the normalised registries (+ the raw payload for the model builder).

    Readers get the object itself — no per-event copying of four dicts; a refresh builds a
    new snapshot and swaps the module reference atomically. Subscriptable like the plain
    dict it replaced (`snap["entity_regs"]`), so fixtures can still be dicts.
    """

    __slots__ = ("floors", "areas", "devices", "entity_regs", "raw", "loaded_at")

    def __init__(
        self,
        floors=(),
        areas: dict | None = None,
        devices: dict | None = None,
        entity_regs: dict | None = None,
        raw: dict | None = None,
        loaded_at: float = 0.0,
    ) -> None:
        object.__setattr__(self, "floors", tuple(floors or ()))
        object.__setattr__(self, "areas", MappingProxyType(dict(areas or {})))
        object.__setattr__(self, "devices", MappingProxyType(dict(devices or {})))
        object.__setattr__(self, "entity_regs", MappingProxyType(dict(entity_regs or {})))
        object.__setattr__(self, "raw", MappingProxyType(dict(raw or {})))
        object.__setattr__(self, "loaded_at", float(loaded_at or 0.0))

    def __setattr__(self, name, value) -> None:
        raise TypeError("RegistrySnapshot is immutable")

    def __getitem__(self, key: str):
        if key not in self.__slots__:
            raise KeyError(key)
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key) if key in self.__slots__ else default

    @property
    def loaded(self) -> bool:
        return self.loaded_at > 0


# Registry cache shared by the model builder, target resolution and the push thread.
# Replaced as a whole (never mutated) — readers take `registry_snapshot()` without a lock.
REGISTRY_CACHE: RegistrySnapshot = RegistrySnapshot()
# Serialises refreshes: a second caller never stacks another HA round-trip on a running one.
REGISTRY_REFRESH_LOCK = threading.Lock()


def refresh_registry_cache(reason: str = "", force_bump: bool = False) -> bool:
    """Reload registries from HA, persist snapshot_version, push {type:"model"} if it bumped.

    `force_bump` (registry-updated events) bumps even when the fingerprint is unchanged.
    One refresh at a time: while another is in flight this call waits for it and reuses
    its result instead of fetching again (a forced bump is still honoured).
    """
    global REGISTRY_CACHE
    if not REGISTRY_REFRESH_LOCK.acquire(blocking=False):
        with REGISTRY_REFRESH_LOCK:
            pass  # the in-flight refresh finished; its snapshot is ours too
        if force_bump:
            push_model_changed(bump_snapshot_version(reason))
        return REGISTRY_CACHE.loaded
    try:
        try:
            raw = fetch_registries()
        except Exception as e:
            print(f"registry refresh failed ({reason or 'periodic'}): {e}", flush=True)
            return False
        floors = normalize_floors(raw["floors"])
        areas = normalize_areas(raw["areas"])
        devices = normalize_devices(raw["devices"])
        regs = normalize_entity_registry(raw["entity_registry"])
        first_load = not REGISTRY_CACHE.loaded
        REGISTRY_CACHE = RegistrySnapshot(
            floors=floors,
            areas=areas,
            devices=devices,
            entity_regs=regs,
            raw=raw,
            loaded_at=time.time(),
        )
        version, bumped = ensure_snapshot_for_fingerprint(
            registry_fingerprint(floors, areas, devices, regs), force=force_bump
        )
        if bumped or first_load:
            # First load: state events were dropped until now → tell clients to refetch.
            push_model_changed(version)
        return True
    finally:
        REGISTRY_REFRESH_LOCK.release()


def registry_snapshot() -> RegistrySnapshot:
    return REGISTRY_CACHE


def registries_for_commands() -> dict:
    """Cached registries, loading them on first use (commands must not wait on the push thread)."""
    snap = registry_snapshot()
    if not snap["loaded_at"]:
        refresh_registry_cache("command")
        snap = registry_snapshot()
    return snap


def fetch_hub_model(payload: dict | None = None) -> dict:
    """arvio.model — HubModelPayload for this hub (paged by entities)."""
    payload = payload if isinstance(payload, dict) else {}
    states = ha("/states")
    if not isinstance(states, list):
        raise RuntimeError(err or "HA states failed")
    cfg = ha("/config") or {}
    if isinstance(cfg, dict):
        HA_INFO["version"] = cfg.get("version") or HA_INFO.get("version")
        HA_INFO["time_zone"] = cfg.get("time_zone") or HA_INFO.get("time_zone")
    refreshed = refresh_registry_cache("model")
    snap = registry_snapshot()
    if not refreshed and not snap["loaded_at"]:
        raise RuntimeError("HA registries unavailable")
    raw = snap.raw
    automations = fetch_arvio_automation_configs(states)
    return build_hub_model(
        states,
        raw.get("floors"),
        raw.get("areas"),
        raw.get("devices"),
        raw.get("entity_registry"),
        automations,
        ha_version=HA_INFO.get("version"),
        timezone_name=HA_INFO.get("time_zone"),
        snapshot_version=get_snapshot_version(),
        offset=payload.get("offset"),
        limit=payload.get("limit"),
        ma_config_entry_id=music_assistant_entry_id(),
    )


# --- Μουσική: art, browse, search (0.1.21, §15.4–15.5) --------------------------

MEDIA_ART_CACHE = LRU(MEDIA_ART_CACHE_SIZE)  # art_hash → {mime, data_base64, reason}


def _pillow_image():
    """Pillow's Image module, or None when not installed (the fallback returns small originals only)."""
    try:
        from PIL import Image  # type: ignore

        return Image
    except Exception:
        return None


def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def resize_art(
    data: bytes,
    image_mod=None,
    *,
    max_px: int = MEDIA_ART_MAX_PX,
    quality: int = MEDIA_ART_JPEG_QUALITY,
    wire_max: int = MEDIA_ART_WIRE_MAX_BYTES,
    no_pillow_max: int = MEDIA_ART_NO_PILLOW_MAX_BYTES,
) -> tuple[bytes | None, str | None, str | None]:
    """→ (bytes, mime, reason). Pure given `image_mod` (None = autodetect Pillow, False = none).

    With Pillow: ≤ max_px JPEG at `quality`, stepping the quality down (80 → 60 → 40) until the
    bytes fit the wire cap. Without Pillow: the original only when ≤ no_pillow_max — and the
    wire cap is a *hard* cap on both paths, so anything above it is `too_large`.
    """
    if image_mod is None:
        image_mod = _pillow_image()
    if not image_mod:
        if len(data) > no_pillow_max or len(data) > wire_max:
            return None, None, "too_large"
        return data, sniff_mime(data), None
    try:
        img = image_mod.open(io.BytesIO(data))
        img.thumbnail((max_px, max_px))
        img = img.convert("RGB")
        q = quality
        while True:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=q, optimize=True)
            out = buf.getvalue()
            if len(out) <= wire_max or q <= 40:
                break
            q -= 20
    except Exception as e:  # undecodable picture (SVG, truncated download, …)
        return None, None, f"decode_failed: {e}"
    if len(out) > wire_max:
        return None, None, "too_large"
    return out, "image/jpeg", None


def fetch_entity_picture(picture: str, timeout: int = 10) -> tuple[bytes, str | None]:
    """entity_picture → (bytes, content-type).

    `/api/...` (HA's media_player_proxy with its per-entity token) goes through the Supervisor
    core proxy with the add-on token; absolute http(s) pictures (Spotify CDN …) are fetched
    directly. Other relative paths are not reachable through the proxy.
    """
    global TOKEN
    headers: dict = {}
    if picture.startswith("/api/"):
        if not TOKEN:
            TOKEN = read_token()
        if not TOKEN:
            raise RuntimeError("missing SUPERVISOR_TOKEN")
        url = "http://supervisor/core/api" + picture[len("/api"):]
        headers["Authorization"] = f"Bearer {TOKEN}"
    elif picture.startswith(("http://", "https://")):
        url = picture
    else:
        raise RuntimeError("unsupported_picture")
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MEDIA_ART_FETCH_MAX_BYTES + 1)
        ctype = r.headers.get("Content-Type")
    if len(raw) > MEDIA_ART_FETCH_MAX_BYTES:
        raise RuntimeError("too_large")
    mime = str(ctype).split(";", 1)[0].strip().lower() if ctype else None
    return raw, mime


def _load_art(picture: str) -> dict:
    try:
        raw, mime = fetch_entity_picture(picture)
    except Exception as e:
        reason = "too_large" if str(e) == "too_large" else f"fetch_failed: {e}"
        return {"mime": None, "data_base64": None, "reason": reason}
    out, out_mime, reason = resize_art(raw)
    if out is None:
        return {"mime": None, "data_base64": None, "reason": reason}
    return {"mime": out_mime or mime, "data_base64": base64.b64encode(out).decode("ascii"), "reason": None}


def media_art(payload: dict, entity_id: str = "") -> dict:
    """arvio.media_art {entity_id} → {entity_id, art_hash, mime, data_base64, reason}. LRU 50 by art_hash."""
    payload = payload if isinstance(payload, dict) else {}
    eid = str(payload.get("entity_id") or entity_id or "")
    if not eid.startswith("media_player."):
        raise ValueError("media_player entity_id required")
    st_obj = ha(f"/states/{eid}")
    if not isinstance(st_obj, dict):
        raise RuntimeError(err or "state read failed")
    attrs = st_obj.get("attributes") if isinstance(st_obj.get("attributes"), dict) else {}
    picture = attrs.get("entity_picture")
    art_hash = media_art_hash(attrs.get("media_content_id"), picture)
    out = {"ok": True, "entity_id": eid, "art_hash": art_hash, "mime": None, "data_base64": None, "reason": None}
    if not art_hash:
        out["reason"] = "no_picture"
        return out
    cached = MEDIA_ART_CACHE.get(art_hash)
    if isinstance(cached, dict):
        out["cached"] = True
    else:
        cached = _load_art(str(picture))
        if not str(cached.get("reason") or "").startswith("fetch_failed"):
            MEDIA_ART_CACHE.put(art_hash, cached)  # transient fetch errors are retried next time
    out.update(cached)
    return out


def browse_item(node: dict) -> dict:
    """BrowseMedia.as_dict() child → app item. Thumbnails: the URL only when a phone can load it
    directly (absolute https); always a hash so the app can key its own cache. Pure."""
    node = node if isinstance(node, dict) else {}
    raw_thumb = node.get("thumbnail")
    raw_thumb = str(raw_thumb) if isinstance(raw_thumb, str) and raw_thumb else None
    return {
        "title": str(node.get("title") or ""),
        "media_content_id": str(node["media_content_id"]) if node.get("media_content_id") not in (None, "") else None,
        "media_content_type": str(node["media_content_type"]) if node.get("media_content_type") else None,
        "media_class": str(node["media_class"]) if node.get("media_class") else None,
        "can_play": bool(node.get("can_play")),
        "can_expand": bool(node.get("can_expand")),
        "thumbnail": raw_thumb if raw_thumb and raw_thumb.startswith("https://") else None,
        "thumbnail_hash": hashlib.sha1(raw_thumb.encode("utf-8")).hexdigest() if raw_thumb else None,
    }


def browse_result_to_page(result, cap: int = MEDIA_BROWSE_MAX_ITEMS) -> dict:
    """media_player/browse_media result → {title, media_content_id, media_content_type, media_class,
    items[≤ cap], total, truncated}. Pure."""
    result = result if isinstance(result, dict) else {}
    children = result.get("children") if isinstance(result.get("children"), list) else []
    items = [browse_item(c) for c in children[:cap] if isinstance(c, dict)]
    head = browse_item(result)
    return {
        "title": head["title"],
        "media_content_id": head["media_content_id"],
        "media_content_type": head["media_content_type"],
        "media_class": head["media_class"],
        "thumbnail": head["thumbnail"],
        "items": items,
        "total": len(children),
        "truncated": len(children) > cap,
    }


def media_browse(payload: dict, entity_id: str = "") -> dict:
    """arvio.media_browse {entity_id, media_content_id?, media_content_type?} via HA WS media_player/browse_media."""
    payload = payload if isinstance(payload, dict) else {}
    eid = str(payload.get("entity_id") or entity_id or "")
    if not eid.startswith("media_player."):
        raise ValueError("media_player entity_id required")
    extra: dict = {"entity_id": eid}
    if payload.get("media_content_id") not in (None, ""):
        extra["media_content_id"] = str(payload["media_content_id"])
    if payload.get("media_content_type"):
        extra["media_content_type"] = str(payload["media_content_type"])
    try:
        result = ha_ws_query_command("media_player/browse_media", extra)
    except HaWsUnavailable as e:
        raise RuntimeError(f"HA websocket unavailable: {e}") from e
    page = browse_result_to_page(result)
    page.update({"ok": True, "entity_id": eid})
    return page


MA_SEARCH_KEYS = (("artists", "artist"), ("albums", "album"), ("tracks", "track"), ("playlists", "playlist"), ("radio", "radio"))


def ma_search_items(response, cap: int = MEDIA_BROWSE_MAX_ITEMS) -> list:
    """music_assistant.search response → items (media_content_id = MA uri, media_content_type = MA media_type). Pure."""
    response = response if isinstance(response, dict) else {}
    items: list = []
    for key, default_type in MA_SEARCH_KEYS:
        for it in response.get(key) or []:
            if not isinstance(it, dict) or not it.get("uri"):
                continue
            mtype = str(it.get("media_type") or default_type)
            image = it.get("image")
            image = str(image) if isinstance(image, str) and image else None
            artists = it.get("artists") if isinstance(it.get("artists"), list) else []
            artist_names = [str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")]
            album = it.get("album") if isinstance(it.get("album"), dict) else None
            items.append(
                {
                    "title": str(it.get("name") or ""),
                    "media_content_id": str(it["uri"]),
                    "media_content_type": mtype,
                    "media_class": mtype,
                    "can_play": True,
                    "can_expand": mtype in ("artist", "album", "playlist"),
                    "thumbnail": image if image and image.startswith("https://") else None,
                    "thumbnail_hash": hashlib.sha1(image.encode("utf-8")).hexdigest() if image else None,
                    "artist": " · ".join(artist_names) or None,
                    "album": str(album.get("name")) if album and album.get("name") else None,
                }
            )
            if len(items) >= cap:
                return items
    return items


def media_search(payload: dict, entity_id: str = "") -> dict:
    """arvio.media_search {entity_id, query, media_type?, media_content_id?, media_content_type?} (§15.5):
    player has SEARCH_MEDIA → HA WS media_player/search_media (media_type → `media_filter_classes`,
    media_content_id/type = the browse node to search within, passed through unchanged);
    else Music Assistant `music_assistant.search` (return_response, `media_type: [media_type]`);
    else {items: [], reason: "search_unavailable"}."""
    payload = payload if isinstance(payload, dict) else {}
    eid = str(payload.get("entity_id") or entity_id or "")
    if not eid.startswith("media_player."):
        raise ValueError("media_player entity_id required")
    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("query required")
    media_type = str(payload.get("media_type") or "").strip() or None
    st_obj = ha(f"/states/{eid}")
    attrs = st_obj.get("attributes") if isinstance(st_obj, dict) and isinstance(st_obj.get("attributes"), dict) else {}
    sf = _int_or_none(attrs.get("supported_features")) or 0
    base = {"ok": True, "entity_id": eid, "query": query}
    if sf & MEDIA_FEATURE["SEARCH_MEDIA"]:
        extra: dict = {"entity_id": eid, "search_query": query}
        if media_type:
            extra["media_filter_classes"] = [media_type]
        if payload.get("media_content_id") not in (None, ""):
            extra["media_content_id"] = str(payload["media_content_id"])
        if payload.get("media_content_type"):
            extra["media_content_type"] = str(payload["media_content_type"])
        try:
            result = ha_ws_query_command("media_player/search_media", extra)
        except HaWsUnavailable as e:
            raise RuntimeError(f"HA websocket unavailable: {e}") from e
        found = result.get("result") if isinstance(result, dict) else result
        found = found if isinstance(found, list) else []
        items = [browse_item(n) for n in found[:MEDIA_BROWSE_MAX_ITEMS] if isinstance(n, dict)]
        return {**base, "source": "ha", "items": items}
    entry_id = MA_INFO.get("entry_id") or music_assistant_entry_id()
    if entry_id:
        data: dict = {"config_entry_id": entry_id, "name": query, "limit": MEDIA_SEARCH_LIMIT}
        if media_type:
            data["media_type"] = [media_type]
        _ctx, response = ha_call_service_response("music_assistant", "search", data)
        return {**base, "source": "music_assistant", "items": ma_search_items(response)}
    return {**base, "source": None, "items": [], "reason": "search_unavailable"}


def media_position_only_change(old_state, new_state) -> bool:
    """True when a media_player state_changed differs only in playback position
    (media_position / media_position_updated_at) — the app extrapolates, no push (§15.6). Pure."""
    if not isinstance(old_state, dict) or not isinstance(new_state, dict):
        return False
    if old_state.get("state") != new_state.get("state"):
        return False

    def stripped(s: dict) -> dict:
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        return {k: v for k, v in attrs.items() if k not in MEDIA_POSITION_ONLY_KEYS}

    return stripped(old_state) == stripped(new_state)


# --- IO: relay push (state / model / heartbeat) --------------------------------

RELAY_WS = None  # websocket.WebSocketApp while the relay WS is open
RELAY_WS_LOCK = threading.Lock()


def relay_send(obj: dict) -> bool:
    """Best-effort send over the relay WS; drops the message when not connected."""
    with RELAY_WS_LOCK:
        ws = RELAY_WS
    if ws is None:
        return False
    try:
        ws.send(json.dumps(obj, default=str))
        return True
    except Exception:
        return False


class Coalescer:
    """Per-key rate limiter: first message goes out at once, later ones wait ≤ interval (latest wins)."""

    def __init__(self, send, interval: float = STATE_COALESCE_S, clock=time.time) -> None:
        self._send = send
        self._interval = interval
        self._clock = clock
        self._last: dict = {}
        self._pending: dict = {}
        self._lock = threading.Lock()

    def offer(self, key: str, msg: dict) -> bool:
        now = self._clock()
        with self._lock:
            due = now - self._last.get(key, 0.0) >= self._interval
            if due and key not in self._pending:
                self._last[key] = now
                immediate = True
            else:
                self._pending[key] = msg
                immediate = False
        if immediate:
            self._send(msg)
        return immediate

    def flush(self) -> int:
        now = self._clock()
        with self._lock:
            keys = [k for k in self._pending if now - self._last.get(k, 0.0) >= self._interval]
            batch = []
            for k in keys:
                batch.append(self._pending.pop(k))
                self._last[k] = now
        for msg in batch:
            self._send(msg)
        return len(batch)

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)


PUSH = Coalescer(relay_send, STATE_COALESCE_S)


def push_model_changed(version: int) -> None:
    PUSH.offer("__model__", {"type": "model", "hub_id": hub_id, "snapshot_version": int(version)})


def state_event_message(event_data: dict, regs: dict) -> dict | None:
    """state_changed event → {type:"state", …} for exposed entities only; None otherwise. Pure."""
    if not isinstance(event_data, dict):
        return None
    new_state = event_data.get("new_state")
    if not isinstance(new_state, dict):
        return None
    eid = str(event_data.get("entity_id") or new_state.get("entity_id") or "")
    model = entity_model_from_state(
        new_state, regs["entity_regs"].get(eid), regs["devices"], regs["areas"]
    )
    if model is None:
        return None
    if model["domain"] == "media_player" and media_position_only_change(event_data.get("old_state"), new_state):
        return None  # §15.6: the app extrapolates the position; art_hash rides in attrs when it changes
    ctx = new_state.get("context") if isinstance(new_state.get("context"), dict) else {}
    context = {
        "id": ctx.get("id"),
        "parent_id": ctx.get("parent_id"),
        "user_id": ctx.get("user_id"),
    }
    command_id = None
    for key in (context["id"], context["parent_id"]):
        if key:
            hit = CONTEXT_TO_COMMAND.get(str(key))
            if hit:
                command_id = hit
                break
    return {
        "type": "state",
        "hub_id": hub_id,
        "entity_id": eid,
        "state": model["state"],
        "attrs": model["attrs"],
        "last_changed": new_state.get("last_changed"),
        "context": context,
        "command_id": command_id,
    }


REGISTRY_EVENT_TYPES = (
    "area_registry_updated",
    "floor_registry_updated",
    "entity_registry_updated",
    "device_registry_updated",
)

_registry_refresh_timer: threading.Timer | None = None
_registry_refresh_lock = threading.Lock()


def schedule_registry_refresh(reason: str, delay: float = 2.0) -> None:
    """Debounced registry reload after registry events (bursts collapse into one)."""
    global _registry_refresh_timer

    def run() -> None:
        # §14.3: a registry event always bumps snapshot_version and pushes {type:"model"}.
        ok = refresh_registry_cache(reason, force_bump=True)
        if not ok:
            # HA unreachable for the reload: still bump so clients refetch when it is back.
            push_model_changed(bump_snapshot_version(reason))

    with _registry_refresh_lock:
        if _registry_refresh_timer is not None:
            _registry_refresh_timer.cancel()
        _registry_refresh_timer = threading.Timer(delay, run)
        _registry_refresh_timer.daemon = True
        _registry_refresh_timer.start()


def handle_ha_event(event: dict) -> None:
    if not isinstance(event, dict):
        return
    etype = str(event.get("event_type") or "")
    if etype == "state_changed":
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        eid = str(data.get("entity_id") or "")
        if eid.split(".", 1)[0] not in HOME_ENTITY_DOMAINS:
            return
        new_state = data.get("new_state")
        if isinstance(new_state, dict):
            note_state_change(eid, new_state)  # commands waiting on this entity
        regs = registry_snapshot()
        if not regs["loaded_at"]:
            # No registry yet → we cannot tell exposed from hidden; refresh_registry_cache
            # pushes {type:"model"} once loaded so clients refetch instead.
            return
        msg = state_event_message(data, regs)
        if msg is not None:
            PUSH.offer(msg["entity_id"], msg)
    elif etype in REGISTRY_EVENT_TYPES:
        schedule_registry_refresh(etype)


def ha_events_loop() -> None:
    """Subscribe to HA events over the websocket; reconnect with backoff + jitter (1 → 60 s)."""
    global STATE_FEED_LIVE
    backoff = Backoff()
    while True:
        if not TOKEN:
            time.sleep(5)
            continue
        ws = None
        try:
            ws = HaWs(timeout=20.0)
            for etype in ("state_changed",) + REGISTRY_EVENT_TYPES:
                # subscribe_events is documented; the *_registry_updated event names are
                # frontend-internal but stable. TODO confirm.
                ws.command("subscribe_events", {"event_type": etype})
            ws.settimeout(60.0)
            backoff.reset()
            STATE_FEED_LIVE = True
            while True:
                try:
                    data = ws.recv()
                except Exception as e:  # websocket timeout → keepalive ping
                    if type(e).__name__ != "WebSocketTimeoutException":
                        raise
                    ws.send("ping")
                    continue
                if data.get("type") == "event":
                    try:
                        handle_ha_event(data.get("event") or {})
                    except Exception as e:
                        print(f"ha event handler error: {e}", flush=True)
        except Exception as e:
            print(f"ha events ws: {e} (retry in {backoff.delay:.0f}s)", flush=True)
        finally:
            STATE_FEED_LIVE = False
            with STATE_FEED:
                STATE_FEED.notify_all()  # waiters fall back to REST polling
            if ws is not None:
                ws.close()
        time.sleep(backoff.next_sleep())


def push_flush_loop() -> None:
    while True:
        time.sleep(0.2)
        try:
            PUSH.flush()
        except Exception:
            pass


def heartbeat_loop() -> None:
    while True:
        time.sleep(HEARTBEAT_INTERVAL_S)
        try:
            relay_send(
                {
                    "type": "heartbeat",
                    "hub_id": hub_id,
                    "snapshot_version": get_snapshot_version(),
                    "agent_version": AGENT_VERSION,
                    "ts": now_iso(),
                }
            )
        except Exception:
            pass


def registry_maintenance_loop() -> None:
    """Initial registry load (HA may still be booting) + periodic safety refresh."""
    while True:
        snap = registry_snapshot()
        age = time.time() - (snap["loaded_at"] or 0.0)
        if not snap["loaded_at"] or age >= REGISTRY_REFRESH_S:
            refresh_registry_cache("periodic")
        time.sleep(15 if not registry_snapshot()["loaded_at"] else 60)


def relay_http(method: str, path: str, body: dict | None = None, timeout: int = 25):
    global relay_ok, relay_err
    if not RELAY_URL:
        raise RuntimeError("relay_url not configured")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{RELAY_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {RELAY_TOKEN}",
            "Content-Type": "application/json",
            "X-Hub-Id": hub_id or "",
            "Connection": "keep-alive",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            relay_ok = True
            relay_err = ""
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except Exception as e:
        relay_ok = False
        relay_err = str(e)
        raise


def _handle_relay_command(cmd: dict) -> None:
    """Relay command → handle_command (safety checks + LRU) → POST /v1/hub/results."""
    cid = str(cmd.get("command_id") or "")
    try:
        ack = handle_command(cmd, via="relay")
    except Exception as e:  # defensive: handle_command already catches
        ack = make_ack(cid, ok=False, error=str(e))
    body = {
        "hub_id": hub_id,
        "command_id": cid,
        "ok": bool(ack.get("ok")),
        "data": ack,
    }
    if not ack.get("ok"):
        body["error"] = str(ack.get("error") or "command failed")
    try:
        relay_http("POST", "/v1/hub/results", body, timeout=10)
    except Exception as e:
        print(f"relay result post failed for {cid}: {e}", flush=True)


def _relay_ws_url() -> str:
    base = (RELAY_URL or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/v1/hub/ws?hub_id={hub_id}"


RELAY_BACKOFF = Backoff()


def handle_relay_message(message: str) -> None:
    """One frame from the relay WS: commands run off the reader thread; `hello` = connected."""
    global relay_ok, relay_err
    try:
        msg = json.loads(message)
    except Exception:
        return
    if not isinstance(msg, dict):
        return
    if msg.get("type") == "command" and isinstance(msg.get("command"), dict):
        # Off the socket reader thread so pushes/pings keep flowing during the
        # wait-for-state window and slow HA calls.
        threading.Thread(
            target=_handle_relay_command, args=(msg["command"],), daemon=True
        ).start()
    elif msg.get("type") == "hello":
        relay_ok = True
        relay_err = ""
        RELAY_BACKOFF.reset()  # a successful hello restarts the reconnect ladder at 1 s


def relay_ws_session() -> None:
    """Block on WebSocket until disconnect. Instant command path (ARV-031)."""
    global relay_ok, relay_err, RELAY_WS
    try:
        import websocket  # type: ignore
    except ImportError as e:
        raise RuntimeError("websocket-client not installed") from e

    done = threading.Event()

    def on_message(_ws, message: str) -> None:
        handle_relay_message(message)

    def on_error(_ws, error) -> None:
        global relay_ok, relay_err
        relay_ok = False
        relay_err = str(error)

    def on_close(_ws, *_args) -> None:
        global RELAY_WS
        with RELAY_WS_LOCK:
            RELAY_WS = None
        done.set()

    def on_open(ws) -> None:
        global relay_ok, relay_err, RELAY_WS
        relay_ok = True
        relay_err = ""
        with RELAY_WS_LOCK:
            RELAY_WS = ws
        relay_send(
            {
                "type": "heartbeat",
                "hub_id": hub_id,
                "snapshot_version": get_snapshot_version(),
                "agent_version": AGENT_VERSION,
                "ts": now_iso(),
            }
        )

        def ping() -> None:
            while not done.is_set():
                try:
                    ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    break
                done.wait(20)

        threading.Thread(target=ping, daemon=True).start()

    app = websocket.WebSocketApp(
        _relay_ws_url(),
        header=[f"Authorization: Bearer {RELAY_TOKEN}", f"X-Hub-Id: {hub_id}"],
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    try:
        app.run_forever(ping_interval=25, ping_timeout=10)
    finally:
        with RELAY_WS_LOCK:
            RELAY_WS = None
        done.set()


def relay_loop(sleep=time.sleep) -> None:
    """Prefer WebSocket; fall back to short long-poll between reconnects.

    Failures back off 1 → 60 s with jitter (RELAY_BACKOFF); a relay `hello` resets it.
    """
    registered = False
    while True:
        if not RELAY_URL or not hub_id:
            sleep(5)
            continue
        try:
            if not registered:
                relay_http(
                    "POST",
                    "/v1/hub/register",
                    {"hub_id": hub_id, "token": RELAY_TOKEN},
                    timeout=10,
                )
                registered = True
            try:
                relay_ws_session()
            except Exception as e:
                relay_err = str(e)
            # Brief long-poll fallback while WS is down.
            body = relay_http(
                "GET",
                f"/v1/hub/commands?hub_id={hub_id}&wait_ms=3000",
                timeout=10,
            )
            cmd = body.get("command") if isinstance(body, dict) else None
            if cmd and isinstance(cmd, dict):
                _handle_relay_command(cmd)
        except Exception:
            registered = False
            sleep(RELAY_BACKOFF.next_sleep())


def lab_bootstrap() -> dict:
    lab = load_lab()
    if lab.get("bootstrapped"):
        return {
            "ok": True,
            "already": True,
            "partner_org_id": lab.get("partner_org_id"),
            "customer_org_id": lab.get("customer_org_id"),
            "site_id": lab.get("site_id"),
        }
    partner_id = "org_partner_lab"
    customer_id = "org_customer_lab"
    site_id = "site_lab_home"
    lab["orgs"][partner_id] = {
        "org_id": partner_id,
        "type": "partner",
        "name": "SFK Lab Partner",
    }
    lab["orgs"][customer_id] = {
        "org_id": customer_id,
        "type": "customer",
        "name": "Lab Customer",
    }
    lab["sites"][site_id] = {
        "site_id": site_id,
        "customer_org_id": customer_id,
        "name": "Lab Home",
    }
    lab["support_grants"][site_id] = partner_id
    lab["partner_org_id"] = partner_id
    lab["customer_org_id"] = customer_id
    lab["site_id"] = site_id
    lab["bootstrapped"] = True
    save_lab(lab)
    return {
        "ok": True,
        "partner_org_id": partner_id,
        "customer_org_id": customer_id,
        "site_id": site_id,
    }


def issue_claim(hid: str) -> dict:
    global hub_state
    st = load_hub()
    if st.get("hub_id") != hid:
        raise ValueError("Hub not found")
    state = st.get("state") or "prepared"
    if state not in ("prepared", "assigned"):
        raise ValueError(f"Cannot issue claim in hub state {state}")
    lab = load_lab()
    token = secrets.token_urlsafe(24)
    claim_id = f"claim_{secrets.token_hex(4)}"
    expires = time.time() + 30 * 60
    claim = {
        "claim_id": claim_id,
        "hub_id": hid,
        "token_hash": sha(token),
        "state": "issued",
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
        "presence_verified": False,
    }
    lab["claims"][claim_id] = claim
    lab["claim_tokens"][claim_id] = token
    save_lab(lab)
    if state == "prepared":
        hub_state = "assigned"
        st["state"] = "assigned"
        save_hub(st)
    return {
        "claim_id": claim_id,
        "token": token,
        "expires_at": claim["expires_at"],
    }


def confirm_presence(claim_id: str, user_code: str) -> dict:
    lab = load_lab()
    claim = lab["claims"].get(claim_id)
    if not claim:
        raise ValueError("Claim not found")
    # Same-process presence: live code + expiry (also persisted hash for audit)
    if time.time() > exp or user_code.strip() != code:
        raise ValueError("Invalid or expired presence code")
    claim["state"] = "presence_pending"
    claim["presence_verified"] = True
    lab["claims"][claim_id] = claim
    save_lab(lab)
    return {"claim_id": claim_id, "state": claim["state"]}


def redeem_claim(claim_id: str, token: str, site_id: str, actor: dict) -> dict:
    global hub_state, hub_id
    lab = load_lab()
    claim = lab["claims"].get(claim_id)
    if not claim:
        raise ValueError("Claim not found")
    if lab["claim_tokens"].get(claim_id) != token:
        raise ValueError("Invalid claim token")
    if not claim.get("presence_verified"):
        raise ValueError("Physical presence required before redeem")
    site = lab["sites"].get(site_id)
    if not site:
        raise ValueError("Site not found")
    org_type = actor.get("org_type", "partner")
    org_id = actor.get("org_id", "")
    if org_type == "partner":
        if lab["support_grants"].get(site_id) != org_id:
            raise ValueError("Forbidden: no support grant for site")
    elif org_type == "customer":
        if site.get("customer_org_id") != org_id:
            raise ValueError("Forbidden")
    elif org_type != "sfk":
        raise ValueError("Forbidden")

    claim["state"] = "redeemed"
    claim["site_id"] = site_id
    claim["actor_id"] = actor.get("user_id", "lab")
    lab["claims"][claim_id] = claim
    save_lab(lab)

    st = load_hub()
    hub_state = "claimed"
    st["state"] = "claimed"
    st["site_id"] = site_id
    st["customer_org_id"] = site["customer_org_id"]
    save_hub(st)
    hub_id = st.get("hub_id")
    return {
        "hub_id": hub_id,
        "serial": st.get("serial"),
        "state": "claimed",
        "site_id": site_id,
        "customer_org_id": site["customer_org_id"],
    }


class H(BaseHTTPRequestHandler):
    """Unauthenticated LAN HTTP on :8099.

    Same-origin only: no CORS grants, so a page on another origin cannot drive the hub
    through the browser. The served ui/home/remote/partner pages fetch relative paths.
    Commands go through the LAN allowlist (LAN_ALLOWED_ACTIONS). The presence-code /
    claim endpoints keep their current shape here — tracked separately as
    «LAN hardening: HA ingress».
    """

    def _j(self, status: int, body: dict) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def _file(self, name: str, ctype: str) -> None:
        path = APP / name
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Plain OPTIONS answer without CORS grants: a cross-origin preflight fails closed.
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/partner", "/partner/"):
            return self._file("partner.html", "text/html; charset=utf-8")
        if path in ("/home", "/home/"):
            return self._file("home.html", "text/html; charset=utf-8")
        if path in ("/remote", "/remote/"):
            return self._file("remote.html", "text/html; charset=utf-8")
        if path.startswith("/health"):
            return self._j(
                200,
                {
                    "ok": True,
                    "ha_ok": ha_ok,
                    "hub_id": hub_id,
                    "state": hub_state,
                    "mode": mode,
                    "agent_version": AGENT_VERSION,
                    "snapshot_version": get_snapshot_version(),
                    "ha_version": HA_INFO.get("version"),
                    "ma_available": bool(MA_INFO.get("entry_id")),
                    "relay_url": RELAY_URL or None,
                    "relay_ok": relay_ok,
                    "relay_error": relay_err,
                    "has_token": bool(TOKEN),
                    "error": err,
                },
            )
        if path.startswith("/api/status"):
            return self._j(
                200,
                {
                    "hub_id": hub_id,
                    "presence_code": code,
                    "ha_ok": ha_ok,
                    "mode": mode,
                    "state": hub_state,
                    "relay_url": RELAY_URL or None,
                    "relay_ok": relay_ok,
                    "entities": entities(),
                    "error": err,
                    "lab": {
                        "bootstrapped": load_lab().get("bootstrapped", False),
                        "site_id": load_lab().get("site_id"),
                        "partner_org_id": load_lab().get("partner_org_id"),
                    },
                },
            )
        if path == "/v1/hubs" or path.startswith("/v1/hubs/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2:
                st = load_hub()
                if not st.get("hub_id"):
                    return self._j(404, {"error": "no hub"})
                return self._j(
                    200,
                    {
                        "hubs": [
                            {
                                "hub_id": st.get("hub_id"),
                                "serial": st.get("serial"),
                                "state": st.get("state"),
                                "site_id": st.get("site_id"),
                            }
                        ]
                    },
                )
            if len(parts) == 3:
                hid = parts[2]
                st = load_hub()
                if st.get("hub_id") != hid:
                    return self._j(404, {"error": "Hub not found"})
                return self._j(
                    200,
                    {
                        "hub_id": hid,
                        "serial": st.get("serial"),
                        "state": st.get("state"),
                        "site_id": st.get("site_id"),
                        "customer_org_id": st.get("customer_org_id"),
                    },
                )
        if path == "/":
            return self._file("ui.html", "text/html; charset=utf-8")
        self._j(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/v1/lab/bootstrap":
                if mode == "remote":
                    boot = cloud_json("POST", "/v1/lab/bootstrap", {})
                    cache_lab_boot(boot)
                    return self._j(200, boot)
                return self._j(200, lab_bootstrap())

            if path.startswith("/api/service/"):
                # /api/service/{domain}/{service}
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("use /api/service/{domain}/{service}")
                body = self._read_json()
                eid = str(body.get("entity_id") or "")
                if not eid:
                    raise ValueError("entity_id required")
                action = f"{parts[2]}.{parts[3]}"
                try:
                    check_command_safety({"action": action, "entity_id": eid}, via="lan")
                except CommandRejected as e:
                    raise ValueError(f"Forbidden: {e.error}: {e}") from e
                return self._j(200, call_service(parts[2], parts[3], {"entity_id": eid}))

            # Local stand-in for relay client API (same shape) — lab /remote UI.
            # Real CGNAT remote uses external @arvio/relay; hub only outbound-polls.
            parts = [p for p in path.split("/") if p]
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "hubs"
                and parts[3] == "commands"
            ):
                body = self._read_json()
                hid = parts[2]
                if hid != hub_id:
                    raise ValueError("Hub not found")
                action = str(body.get("action") or "")
                eid = str(body.get("entity_id") or "")
                # Same safety checks as the relay path, plus the LAN allowlist
                # (LAN_ALLOWED_ACTIONS: no security actions / arvio writes / batch / target).
                # Payload is not forwarded here on purpose: confirm_dangerous can only
                # come from the cloud.
                cid = f"local_{secrets.token_hex(4)}"
                cmd: dict = {"command_id": cid, "action": action, "entity_id": eid}
                if action in MEDIA_ACTIONS:
                    # §15.5: music payloads (volume / source / queue / query) are harmless; only
                    # LAN_MEDIA_PAYLOAD_KEYS pass — never confirm_dangerous / code / target.
                    raw_payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
                    lan_payload = {k: v for k, v in raw_payload.items() if k in LAN_MEDIA_PAYLOAD_KEYS}
                    for key in LAN_MEDIA_PAYLOAD_KEYS:
                        if key in body and key not in lan_payload:
                            lan_payload[key] = body[key]
                    cmd["payload"] = lan_payload
                ack = handle_command(cmd, via="lan")
                if not ack.get("ok"):
                    msg = str(ack.get("error") or "command failed")
                    code = ack.get("error_code") or ack.get("error")
                    if code in ("lan_forbidden", "confirm_required", "action_not_allowed"):
                        raise ValueError(f"Forbidden: {msg}")
                    raise ValueError(msg)
                return self._j(200, {"via": "local-agent", **ack})
            # POST /v1/hubs/{id}/claims
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "hubs"
                and parts[3] == "claims"
            ):
                if mode == "remote":
                    out = cloud_json("POST", f"/v1/hubs/{parts[2]}/claims", {})
                    sync_hub_from_cloud({"state": "assigned"})
                    return self._j(200, out)
                return self._j(200, issue_claim(parts[2]))

            # POST /v1/claims/{id}/presence
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "claims"
                and parts[3] == "presence"
            ):
                body = self._read_json()
                if mode == "remote":
                    out = cloud_json(
                        "POST",
                        f"/v1/claims/{parts[2]}/presence",
                        {"code": str(body.get("code") or "")},
                    )
                    return self._j(200, out)
                return self._j(
                    200, confirm_presence(parts[2], str(body.get("code") or ""))
                )

            # POST /v1/claims/{id}/redeem
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "claims"
                and parts[3] == "redeem"
            ):
                body = self._read_json()
                if mode == "remote":
                    payload = {
                        "token": str(body.get("token") or ""),
                        "site_id": str(body.get("site_id") or ""),
                        "user_id": str(body.get("user_id") or "tech_lab"),
                        "org_id": str(body.get("org_id") or ""),
                        "org_type": str(body.get("org_type") or "partner"),
                    }
                    hub = cloud_json(
                        "POST", f"/v1/claims/{parts[2]}/redeem", payload
                    )
                    sync_hub_from_cloud(hub)
                    return self._j(200, hub)
                actor = {
                    "user_id": str(body.get("user_id") or "tech_lab"),
                    "org_id": str(body.get("org_id") or ""),
                    "org_type": str(body.get("org_type") or "partner"),
                }
                return self._j(
                    200,
                    redeem_claim(
                        parts[2],
                        str(body.get("token") or ""),
                        str(body.get("site_id") or ""),
                        actor,
                    ),
                )

            self._j(404, {"error": "not found"})
        except Exception as e:
            msg = str(e)
            status = 403 if msg.startswith("Forbidden") else 400
            self._j(status, {"error": msg})

    def log_message(self, *_args) -> None:
        pass


def loop() -> None:
    while True:
        rotate()
        cfg = ha("/config")
        if isinstance(cfg, dict):
            HA_INFO["version"] = cfg.get("version") or HA_INFO.get("version")
            HA_INFO["time_zone"] = cfg.get("time_zone") or HA_INFO.get("time_zone")
        enroll()
        time.sleep(30)


if __name__ == "__main__":
    TOKEN = read_token()
    opts()
    rotate()
    enroll()
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=relay_loop, daemon=True).start()
    # 0.1.20: registry cache, HA event push, coalescer flush, relay heartbeat.
    threading.Thread(target=registry_maintenance_loop, daemon=True).start()
    threading.Thread(target=ha_events_loop, daemon=True).start()
    threading.Thread(target=push_flush_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    print(
        f"arvio-agent :{PORT} mode={mode} hub={hub_id} relay={RELAY_URL or 'off'} token={'yes' if TOKEN else 'NO'}",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
