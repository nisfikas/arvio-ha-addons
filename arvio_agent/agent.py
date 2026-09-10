#!/usr/bin/env python3
"""Arvio Agent — HA peek, embedded/remote enroll, claim lab API, Home controls."""
from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DATA = Path("/data")
DATA.mkdir(parents=True, exist_ok=True)
STATE = DATA / "hub.json"
LAB = DATA / "lab_store.json"
APP = Path("/app")

CLOUD = "https://cloud.arvio.systems"
SERIAL = "rpi-lab-1"
PORT = 8099
RELAY_URL = "https://relay.arvio.systems"
RELAY_TOKEN = "lab-relay-token"
AGENT_VERSION = "0.1.19"
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


def ha_ws_command(msg_type: str, extra: dict | None = None, timeout: float = 20.0):
    """Call a Home Assistant websocket command (blueprints / device registry)."""
    try:
        import websocket  # type: ignore
    except ImportError as e:
        raise RuntimeError("websocket-client not installed") from e
    if not TOKEN:
        raise RuntimeError("missing SUPERVISOR_TOKEN")
    url = "ws://supervisor/core/websocket"
    ws = websocket.create_connection(url, timeout=timeout)
    try:
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected HA ws hello: {hello}")
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"HA ws auth failed: {auth}")
        payload = {"id": 1, "type": msg_type}
        if extra:
            payload.update(extra)
        ws.send(json.dumps(payload))
        while True:
            raw = ws.recv()
            data = json.loads(raw)
            if data.get("id") != 1:
                continue
            if data.get("type") == "result":
                if not data.get("success"):
                    err_obj = data.get("error") or {}
                    raise RuntimeError(
                        str(err_obj.get("message") or err_obj or "ws command failed")
                    )
                return data.get("result")
            raise RuntimeError(f"unexpected HA ws response: {data}")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def zha_is_available() -> bool:
    """True when a non-disabled ZHA config entry exists (documented Core API)."""
    entries = ha("/config/config_entries/entry")
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            if e.get("domain") != "zha":
                continue
            if e.get("disabled_by"):
                continue
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
    eid = str(entity_id or "").strip()
    if eid.startswith("automation."):
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
            )
        ):
            continue
        attrs = e.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
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
        out.append(item)
    return out[:120]


def call_service(domain: str, service: str, data: dict) -> dict:
    result = ha(
        f"/services/{domain}/{service}",
        method="POST",
        body=data,
    )
    if result is None:
        raise RuntimeError(err or "HA service failed")
    entity_id = str(data.get("entity_id") or "")

    expected = None
    if service == "turn_on":
        expected = "on"
    elif service == "turn_off":
        expected = "off"
    elif service == "open_cover":
        expected = "open"
    elif service == "close_cover":
        expected = "closed"

    st = None
    state = None
    attrs: dict = {}
    # Zigbee/Wi‑Fi devices often lag; don't report stale pre-command state.
    deadline = time.time() + 1.4
    while True:
        st = ha(f"/states/{entity_id}") if entity_id else None
        state = st.get("state") if isinstance(st, dict) else None
        attrs = (st.get("attributes") if isinstance(st, dict) else None) or {}
        if not isinstance(attrs, dict):
            attrs = {}
        if expected is None or state is None:
            break
        if domain == "climate" and expected == "on":
            if state not in ("off", "unavailable", "unknown"):
                break
        elif domain == "climate" and expected == "off":
            if state == "off":
                break
        elif state == expected:
            break
        if time.time() >= deadline:
            # Fall back to intended state so clients don't revert optimistic UI.
            if expected is not None and (
                state in (None, "unavailable", "unknown")
                or (expected == "on" and state == "off")
                or (expected == "off" and state == "on")
                or (expected == "open" and state == "closed")
                or (expected == "closed" and state == "open")
            ):
                state = expected
            break
        time.sleep(0.15)

    return {
        "ok": True,
        "entity_id": entity_id,
        "service": f"{domain}.{service}",
        "state": state,
        "brightness": attrs.get("brightness") if isinstance(attrs, dict) else None,
        "rgb_color": attrs.get("rgb_color") if isinstance(attrs, dict) else None,
        "temperature": attrs.get("temperature") if isinstance(attrs, dict) else None,
        "hvac_mode": attrs.get("hvac_mode") if isinstance(attrs, dict) else None,
        "fan_mode": attrs.get("fan_mode") if isinstance(attrs, dict) else None,
        "preset_mode": attrs.get("preset_mode") if isinstance(attrs, dict) else None,
        "swing_mode": attrs.get("swing_mode") if isinstance(attrs, dict) else None,
        "current_position": attrs.get("current_position")
        if isinstance(attrs, dict)
        else None,
    }


def execute_action(
    action: str, entity_id: str, payload: dict | None = None
) -> dict:
    """action like light.turn_on — remote/relay command path."""
    payload = payload if isinstance(payload, dict) else {}
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
    if action == "backup.create":
        name = str(payload.get("name") or "") or None
        out = create_full_backup(name)
        # Refresh list after kickoff (job may still be running).
        tel = backup_telemetry()
        out.update({"backup_count": tel["backup_count"], "last_backup_at": tel["last_backup_at"]})
        return out
    if action == "agent.update":
        return apply_agent_update(payload)
    if action in ("lock.unlock", "lock.open", "alarm.disarm", "door.open"):
        pass
    if "." not in action:
        raise ValueError("action must be domain.service")
    domain, service = action.split(".", 1)
    if not entity_id and action != "arvio.list_entities":
        raise ValueError("entity_id required")

    data: dict = {"entity_id": entity_id}
    if action == "light.turn_on":
        if payload.get("brightness") is not None:
            data["brightness"] = int(payload["brightness"])
        if payload.get("brightness_pct") is not None:
            data["brightness_pct"] = int(payload["brightness_pct"])
        if payload.get("rgb_color") is not None:
            data["rgb_color"] = payload["rgb_color"]
        if payload.get("color_temp") is not None:
            data["color_temp"] = int(payload["color_temp"])
        if payload.get("hs_color") is not None:
            data["hs_color"] = payload["hs_color"]
    elif action == "climate.set_temperature":
        if payload.get("temperature") is not None:
            data["temperature"] = float(payload["temperature"])
        if payload.get("hvac_mode") is not None:
            data["hvac_mode"] = str(payload["hvac_mode"])
    elif action == "climate.set_hvac_mode":
        data["hvac_mode"] = str(payload.get("hvac_mode") or service)
        service = "set_hvac_mode"
    elif action == "climate.set_fan_mode":
        data["fan_mode"] = str(payload.get("fan_mode") or "")
        service = "set_fan_mode"
    elif action == "climate.set_preset_mode":
        data["preset_mode"] = str(payload.get("preset_mode") or "")
        service = "set_preset_mode"
    elif action == "climate.set_swing_mode":
        data["swing_mode"] = str(payload.get("swing_mode") or "")
        service = "set_swing_mode"
    elif action == "cover.set_cover_position":
        data["position"] = int(payload.get("position") or 0)
        service = "set_cover_position"

    return call_service(domain, service, data)


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
    cid = str(cmd.get("command_id") or "")
    action = str(cmd.get("action") or "")
    entity_id = str(cmd.get("entity_id") or "")
    payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
    try:
        out = execute_action(action, entity_id, payload)
        relay_http(
            "POST",
            "/v1/hub/results",
            {
                "hub_id": hub_id,
                "command_id": cid,
                "ok": True,
                "data": out if isinstance(out, dict) else {"ok": True},
            },
            timeout=10,
        )
    except Exception as e:
        relay_http(
            "POST",
            "/v1/hub/results",
            {
                "hub_id": hub_id,
                "command_id": cid,
                "ok": False,
                "error": str(e),
            },
            timeout=10,
        )


def _relay_ws_url() -> str:
    base = (RELAY_URL or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/v1/hub/ws?hub_id={hub_id}"


def relay_ws_session() -> None:
    """Block on WebSocket until disconnect. Instant command path (ARV-031)."""
    global relay_ok, relay_err
    try:
        import websocket  # type: ignore
    except ImportError as e:
        raise RuntimeError("websocket-client not installed") from e

    done = threading.Event()

    def on_message(_ws, message: str) -> None:
        try:
            msg = json.loads(message)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "command" and isinstance(msg.get("command"), dict):
            _handle_relay_command(msg["command"])
        elif msg.get("type") == "hello":
            relay_ok = True
            relay_err = ""

    def on_error(_ws, error) -> None:
        global relay_ok, relay_err
        relay_ok = False
        relay_err = str(error)

    def on_close(_ws, *_args) -> None:
        done.set()

    def on_open(ws) -> None:
        global relay_ok, relay_err
        relay_ok = True
        relay_err = ""

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
    app.run_forever(ping_interval=25, ping_timeout=10)
    done.set()


def relay_loop() -> None:
    """Prefer WebSocket; fall back to short long-poll between reconnects."""
    registered = False
    while True:
        if not RELAY_URL or not hub_id:
            time.sleep(5)
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
            time.sleep(1)


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
    def _j(self, status: int, body: dict) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
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
                return self._j(200, call_service(parts[2], parts[3], eid))

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
                out = execute_action(action, eid)
                return self._j(
                    200,
                    {
                        "command_id": f"local_{secrets.token_hex(4)}",
                        "ok": True,
                        "via": "local-agent",
                        **out,
                    },
                )
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
        ha("/config")
        enroll()
        time.sleep(30)


if __name__ == "__main__":
    TOKEN = read_token()
    opts()
    rotate()
    enroll()
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=relay_loop, daemon=True).start()
    print(
        f"arvio-agent :{PORT} mode={mode} hub={hub_id} relay={RELAY_URL or 'off'} token={'yes' if TOKEN else 'NO'}",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
