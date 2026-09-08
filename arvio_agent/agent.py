#!/usr/bin/env python3
"""Minimal Arvio Agent for HAOS lab."""
from __future__ import annotations
import hashlib, json, os, random, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path("/data"); DATA.mkdir(parents=True, exist_ok=True)
STATE = DATA / "hub.json"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
CLOUD = "http://192.168.68.71:8787"
SERIAL = "rpi-lab-1"
PORT = 8099
code = "000000"
exp = 0.0
hub_id = None
ha_ok = False
err = ""

def opts():
    global CLOUD, SERIAL
    p = DATA / "options.json"
    if p.exists():
        o = json.loads(p.read_text())
        CLOUD = str(o.get("cloud_url") or CLOUD).rstrip("/")
        if o.get("serial"): SERIAL = str(o["serial"])

def save(s): STATE.write_text(json.dumps(s, indent=2))
def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {}

def ha(path):
    global ha_ok, err
    if not TOKEN:
        err = "no SUPERVISOR_TOKEN"; ha_ok = False; return None
    req = urllib.request.Request(
        f"http://supervisor/core/api{path}",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            ha_ok = True
            return json.loads(r.read().decode())
    except Exception as e:
        ha_ok = False; err = str(e); return None

def rotate():
    global code, exp
    code = f"{random.randint(0,999999):06d}"
    exp = time.time() + 300

def enroll():
    global hub_id, err
    st = load(); pk = st.get("enroll_public_key") or os.urandom(8).hex()
    st.update({"enroll_public_key": pk, "serial": SERIAL}); save(st)
    try:
        req = urllib.request.Request(
            f"{CLOUD}/v1/hubs/enroll",
            data=json.dumps({"serial": SERIAL, "enroll_public_key": pk}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read().decode()); hub_id = body.get("hub_id")
            st["hub_id"] = hub_id; save(st)
    except Exception as e:
        err = f"enroll:{e}"; hub_id = st.get("hub_id"); return
    if not hub_id: return
    try:
        payload = {
            "presence_code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "presence_expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)),
        }
        req = urllib.request.Request(
            f"{CLOUD}/v1/hubs/{hub_id}/heartbeat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        err = f"heartbeat:{e}"

def entities():
    st = ha("/states")
    if not isinstance(st, list): return []
    out = []
    for e in st:
        eid = str(e.get("entity_id", ""))
        if eid.startswith(("light.", "switch.", "cover.", "climate.")):
            out.append({"entity_id": eid, "state": e.get("state"),
                        "name": (e.get("attributes") or {}).get("friendly_name", eid)})
    return out[:80]

class H(BaseHTTPRequestHandler):
    def _j(self, c, b):
        d = json.dumps(b).encode()
        self.send_response(c); self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(d)
    def do_GET(self):
        if self.path.startswith("/health"):
            return self._j(200, {"ok": True, "ha_ok": ha_ok, "hub_id": hub_id, "error": err})
        if self.path.startswith("/api/status"):
            return self._j(200, {"hub_id": hub_id, "presence_code": code, "ha_ok": ha_ok,
                                 "entities": entities(), "error": err})
        html = f"""<!doctype html><html lang=el><meta charset=utf-8>
<title>Arvio</title><body style="font-family:serif;background:#f3efe6;padding:1.5rem">
<h1>Arvio</h1><p>Κωδικός παρουσίας</p>
<p style="font-size:2rem;letter-spacing:.3em;color:#0f6a5a" id=c>---</p>
<p id=m></p><ul id=l></ul>
<script>
async function r(){{const d=await(await fetch('/api/status')).json();
c.textContent=d.presence_code;m.textContent=d.ha_ok?'HA OK · '+d.hub_id:d.error;
l.innerHTML=(d.entities||[]).map(e=>'<li>'+e.name+' — '+e.state+'</li>').join('')||'<li>no devices</li>'}}
r();setInterval(r,5000)</script></body></html>"""
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers(); self.wfile.write(html.encode())
    def log_message(self, *a): pass

def loop():
    while True:
        rotate(); ha("/config"); enroll(); time.sleep(30)

if __name__ == "__main__":
    opts(); rotate()
    threading.Thread(target=loop, daemon=True).start()
    print(f"arvio-agent :{PORT} cloud={CLOUD}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
