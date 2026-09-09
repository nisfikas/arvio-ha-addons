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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DATA = Path("/data")
DATA.mkdir(parents=True, exist_ok=True)
STATE = DATA / "hub.json"
LAB = DATA / "lab_store.json"
APP = Path("/app")

CLOUD = "https://arvio-cloud.vercel.app"
SERIAL = "rpi-lab-1"
PORT = 8099
RELAY_URL = "https://arvio-cloud.vercel.app"
RELAY_TOKEN = "lab-relay-token"

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


def ha(path: str, method: str = "GET", body: dict | None = None):
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
        with urllib.request.urlopen(req, timeout=10) as r:
            ha_ok = True
            raw = r.read().decode()
            if not err.startswith("enroll:") and not err.startswith("heartbeat:"):
                err = ""
            return json.loads(raw) if raw else {}
    except Exception as e:
        ha_ok = False
        err = str(e)
        return None


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
                    "domain": eid.split(".", 1)[0],
                }
            )
    return out[:80]


def call_service(domain: str, service: str, entity_id: str) -> dict:
    result = ha(
        f"/services/{domain}/{service}",
        method="POST",
        body={"entity_id": entity_id},
    )
    if result is None:
        raise RuntimeError(err or "HA service failed")
    return {"ok": True, "entity_id": entity_id, "service": f"{domain}.{service}"}


def execute_action(action: str, entity_id: str) -> dict:
    """action like light.turn_on — remote/relay command path."""
    if action in ("lock.unlock", "lock.open", "alarm.disarm", "door.open"):
        # Lab: still allow but mark; production TTL enforced at relay
        pass
    if "." not in action:
        raise ValueError("action must be domain.service")
    domain, service = action.split(".", 1)
    if not entity_id:
        raise ValueError("entity_id required")
    return call_service(domain, service, entity_id)


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


def relay_loop() -> None:
    """Outbound long-poll — hub never opens inbound ports."""
    while True:
        if not RELAY_URL or not hub_id:
            time.sleep(5)
            continue
        try:
            relay_http(
                "POST",
                "/v1/hub/register",
                {"hub_id": hub_id, "token": RELAY_TOKEN},
                timeout=10,
            )
            body = relay_http(
                "GET",
                f"/v1/hub/commands?hub_id={hub_id}&wait_ms=20000",
                timeout=25,
            )
            cmd = body.get("command") if isinstance(body, dict) else None
            if not cmd:
                continue
            cid = str(cmd.get("command_id") or "")
            action = str(cmd.get("action") or "")
            entity_id = str(cmd.get("entity_id") or "")
            try:
                execute_action(action, entity_id)
                relay_http(
                    "POST",
                    "/v1/hub/results",
                    {"hub_id": hub_id, "command_id": cid, "ok": True},
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
        except Exception:
            time.sleep(3)


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
                return self._j(200, issue_claim(parts[2]))

            # POST /v1/claims/{id}/presence
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "claims"
                and parts[3] == "presence"
            ):
                body = self._read_json()
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
