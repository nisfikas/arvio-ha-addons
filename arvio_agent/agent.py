#!/usr/bin/env python3
"""Arvio Agent for HAOS lab — presence code, HA peek, cloud/embedded enroll."""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path("/data")
DATA.mkdir(parents=True, exist_ok=True)
STATE = DATA / "hub.json"
UI = Path("/app/ui.html")

# "embedded" = local in-addon lab cloud (no Mac/LAN required)
CLOUD = "embedded"
SERIAL = "rpi-lab-1"
PORT = 8099

code = "000000"
exp = 0.0
hub_id = None
ha_ok = False
err = ""
mode = "embedded"


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


TOKEN = read_token()


def opts() -> None:
    global CLOUD, SERIAL, mode
    p = DATA / "options.json"
    if not p.exists():
        mode = "embedded" if CLOUD in ("", "embedded", "local") else "remote"
        return
    o = json.loads(p.read_text())
    raw = str(o.get("cloud_url") or CLOUD).rstrip("/")
    CLOUD = raw
    mode = "embedded" if raw in ("", "embedded", "local") else "remote"
    if o.get("serial"):
        SERIAL = str(o["serial"])


def save(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def ha(path: str):
    global ha_ok, err, TOKEN
    if not TOKEN:
        TOKEN = read_token()
    if not TOKEN:
        err = "missing SUPERVISOR_TOKEN"
        ha_ok = False
        return None
    req = urllib.request.Request(
        f"http://supervisor/core/api{path}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            ha_ok = True
            if not err.startswith("enroll:") and not err.startswith("heartbeat:"):
                err = ""
            return json.loads(r.read().decode())
    except Exception as e:
        ha_ok = False
        err = str(e)
        return None


def rotate() -> None:
    global code, exp
    code = f"{random.randint(0, 999999):06d}"
    exp = time.time() + 300


def enroll_embedded() -> None:
    """Lab cloud inside the add-on — works when Pi cannot reach Mac."""
    global hub_id, err
    st = load()
    pk = st.get("enroll_public_key") or os.urandom(8).hex()
    hid = st.get("hub_id") or f"hub_lab_{SERIAL.replace('-', '_')}"
    st.update(
        {
            "enroll_public_key": pk,
            "serial": SERIAL,
            "hub_id": hid,
            "mode": "embedded",
            "presence_code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "presence_expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)
            ),
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    save(st)
    hub_id = hid
    if err.startswith("enroll:") or err.startswith("heartbeat:"):
        err = ""


def enroll_remote() -> None:
    global hub_id, err
    st = load()
    pk = st.get("enroll_public_key") or os.urandom(8).hex()
    st.update({"enroll_public_key": pk, "serial": SERIAL})
    save(st)
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
            st["hub_id"] = hub_id
            save(st)
    except Exception as e:
        err = f"enroll:{e}"
        hub_id = st.get("hub_id")
        return
    if not hub_id:
        return
    try:
        payload = {
            "presence_code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "presence_expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)
            ),
        }
        req = urllib.request.Request(
            f"{CLOUD}/v1/hubs/{hub_id}/heartbeat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8).read()
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
        if eid.startswith(("light.", "switch.", "cover.", "climate.")):
            out.append(
                {
                    "entity_id": eid,
                    "state": e.get("state"),
                    "name": (e.get("attributes") or {}).get(
                        "friendly_name", eid
                    ),
                }
            )
    return out[:80]


class H(BaseHTTPRequestHandler):
    def _j(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            return self._j(
                200,
                {
                    "ok": True,
                    "ha_ok": ha_ok,
                    "hub_id": hub_id,
                    "mode": mode,
                    "has_token": bool(TOKEN),
                    "error": err,
                },
            )
        if self.path.startswith("/api/status"):
            return self._j(
                200,
                {
                    "hub_id": hub_id,
                    "presence_code": code,
                    "ha_ok": ha_ok,
                    "mode": mode,
                    "entities": entities(),
                    "error": err,
                },
            )
        html = (
            UI.read_text(encoding="utf-8")
            if UI.exists()
            else "<h1>Arvio</h1><p>ui.html missing</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *_args) -> None:
        pass


def loop() -> None:
    while True:
        rotate()
        ha("/config")
        enroll()
        time.sleep(30)


if __name__ == "__main__":
    opts()
    rotate()
    enroll()
    threading.Thread(target=loop, daemon=True).start()
    print(
        f"arvio-agent :{PORT} mode={mode} cloud={CLOUD} token={'yes' if TOKEN else 'NO'}",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
