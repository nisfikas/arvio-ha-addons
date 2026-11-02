"""Wall-panel installer: the hub finds Android panels on its LAN, installs the Arvio Panel app over
ADB, hands it a one-time pairing token and closes ADB again (arvio docs/ADR/012, plan §5–§7).

Only network and adb work lives here; agent.py wires it to relay-only actions (never the LAN page
on :8099, which has no auth) and keeps screens.json. `adb` is the platform binary; tests pass a fake.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ADB_PORT = 5555
PACKAGE = "systems.arvio.panel"
APP_MAIN = f"{PACKAGE}/.Main"
APP_ADMIN = f"{PACKAGE}/.Admin"
SONOFF_PACKAGE = "com.eWeLinkControlPanel"
MIN_SDK = 26
MIN_WEBVIEW = 88  # panel.html uses aspect-ratio, inset and :is()
MAX_PREFIX = 22  # never sweep more than a /22 (1022 hosts)
PAIR_WAIT_S = 60
PAIR_POLL_S = 1.0
CLOSE_WAIT_S = 10
RETRY_WINDOW_S = 15 * 60  # a failed install leaves ADB open this long for «Δοκίμασε ξανά»

# One shell round trip per panel; every line is key=value.
IDENTIFY_SCRIPT = "; ".join(
    [
        'echo "manufacturer=$(getprop ro.product.manufacturer)"',
        'echo "model=$(getprop ro.product.model)"',
        'echo "android=$(getprop ro.build.version.release)"',
        'echo "sdk=$(getprop ro.build.version.sdk)"',
        'echo "serial=$(getprop ro.serialno)"',
        'echo "mac=$(cat /sys/class/net/wlan0/address 2>/dev/null)"',
        f'echo "sonoff=$(pm path {SONOFF_PACKAGE} 2>/dev/null | head -1)"',
        f'echo "app=$(dumpsys package {PACKAGE} | grep -m1 versionName)"',
        "echo \"webview=$(dumpsys webviewupdate | grep -m1 'Current WebView package')\"",
    ]
)


class InstallError(Exception):
    """A stable code for Partner, e.g. panel_changed, install_failed_older_sdk, pair_timeout."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Adb:
    """The platform `adb` binary. Its key lives in `home`, per hub (never shared, never backed up)."""

    def __init__(self, home: Path, binary: str = "adb"):
        self.home = Path(home)
        self.binary = binary

    def available(self) -> bool:
        # A standalone agent's OTA brings new code, not new image packages: adb may be missing.
        return shutil.which(self.binary) is not None

    def __call__(self, *args: str, serial: str | None = None, timeout: float = 30) -> tuple[int, str]:
        self.home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, HOME=str(self.home), ANDROID_USER_HOME=str(self.home / ".android"))
        cmd = [self.binary, *(["-s", serial] if serial else []), *args]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 255, str(e)
        return p.returncode, (p.stdout or "") + (p.stderr or "")


def serial_hash(serial: str) -> str:
    return hashlib.sha256(f"arvio.panel|{serial}".encode("utf-8")).hexdigest()[:16]


def tcp_open(ip: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- network


def lan_interface(supervisor_info, ha_url: str, env_ip: str, route_ip: str | None):
    """The hub's own LAN address, by deployment (plan §5):
    HAOS add-on → the Supervisor's primary interface (the add-on itself is on a bridge);
    ARVIO_LAN_IP when set; HA Container sidecar → the host in ARVIO_HA_URL; Arvio Core on host
    network → the address the default route uses. None = unknown (Partner then passes a subnet)."""
    data = (supervisor_info or {}).get("data", supervisor_info) if isinstance(supervisor_info, dict) else None
    for iface in sorted((data or {}).get("interfaces") or [], key=lambda i: not i.get("primary")):
        for addr in ((iface.get("ipv4") or {}).get("address") or []):
            try:
                found = ipaddress.ip_interface(addr)
            except ValueError:
                continue
            if found.ip.is_private and not found.ip.is_loopback:
                return found
    for candidate in (env_ip, urlparse(ha_url or "").hostname, route_ip):
        try:
            ip = ipaddress.ip_address(candidate or "")
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback:
            return ipaddress.ip_interface(f"{ip}/24")
    return None


def route_ip() -> str | None:
    """Source address of the default route (UDP connect sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def scan_network(subnet: str | None, lan) -> ipaddress.IPv4Network:
    """What «Βρες πάνελ» sweeps: Partner's subnet or the hub's own, private, at most a /22."""
    if subnet:
        try:
            net = ipaddress.ip_network(str(subnet), strict=False)
        except ValueError:
            raise ValueError("invalid_subnet")
        if net.version != 4 or not net.is_private or net.prefixlen < MAX_PREFIX or net.prefixlen > 30:
            raise ValueError("invalid_subnet")
        return net
    if lan is None:
        raise ValueError("lan_unknown")
    return ipaddress.ip_interface(f"{lan.ip}/{max(lan.network.prefixlen, MAX_PREFIX)}").network


def scan_hosts(net, probe=None) -> list[str]:
    """Hosts with the ADB port open (TCP connect only, 64 at a time: ~3 s for a /24)."""
    probe = probe or tcp_open
    hosts = [str(h) for h in net.hosts()]
    with ThreadPoolExecutor(max_workers=64) as pool:
        hits = pool.map(lambda h: h if probe(h, ADB_PORT) else None, hosts)
    return [h for h in hits if h]


def seen_from_panel(adb, ip: str) -> list[str]:
    """Who is connected to the panel's ADB port: the hub's LAN address as the panel sees it.
    /proc/net/tcp*, not netstat (toybox netstat printed nothing on the NSPanel Pro): local
    :15B3 (5555), state 01 (established), IPv4 as little-endian hex, IPv4-mapped in tcp6."""
    _, out = adb("shell", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null", serial=f"{ip}:{ADB_PORT}")
    seen = []
    for remote in re.findall(r":%04X ([0-9A-F]{8}|0{16}FFFF0{4}[0-9A-F]{8}):[0-9A-F]{4} 01 " % ADB_PORT, out):
        seen.append(socket.inet_ntoa(bytes.fromhex(remote[-8:])[::-1]))
    return list(dict.fromkeys(seen))


# ---------------------------------------------------------------- one panel


def identify(adb, ip: str) -> dict:
    target = f"{ip}:{ADB_PORT}"
    adb("connect", target, timeout=10)
    rc, state = adb("get-state", serial=target, timeout=10)
    state = state.strip()
    if rc != 0 or state != "device":
        return {"ip": ip, "state": "unauthorized" if "unauthorized" in state else "offline"}
    _, out = adb("shell", IDENTIFY_SCRIPT, serial=target)
    raw = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    raw = {k.strip(): v.strip() for k, v in raw.items()}
    web = re.search(r",\s*([\d.]+)\)", raw.get("webview", ""))
    app = re.search(r"versionName=(\S+)", raw.get("app", ""))
    sdk = int(raw["sdk"]) if raw.get("sdk", "").isdigit() else 0
    sonoff = raw.get("sonoff", "").startswith("package:")
    model, maker = raw.get("model", ""), raw.get("manufacturer", "")
    webview = web.group(1) if web else None
    return {
        "ip": ip,
        "state": "device",
        # The NSPanel Pro calls itself px30_evb (a Rockchip board): Sonoff's own app says what it is.
        "label": "NSPanel Pro" if sonoff and model == "px30_evb" else f"{maker.title()} {model}".strip(),
        "android": raw.get("android") or None,
        "sdk": sdk,
        "webview": webview,
        "supported": sdk >= MIN_SDK and webview is not None and int(webview.split(".")[0]) >= MIN_WEBVIEW,
        "arvio_app": app.group(1) if app else None,
        "serial_hash": serial_hash(raw.get("serial", "")),
        "mac": raw.get("mac") or None,
    }


def public(info: dict) -> dict:
    """What Partner may see of a scanned panel: no raw serial, no MAC."""
    return {k: v for k, v in info.items() if k not in ("mac",)}


def install(adb, ip: str, *, serial_hash: str, apk: Path, hub_url: str, hub_id: str, mint_token, paired,
            step, probe=None, sleep=None) -> dict:
    """§6 steps 1–7. `step(name)` reports progress; raises InstallError with a stable code."""
    sleep = sleep or time.sleep
    target = f"{ip}:{ADB_PORT}"
    info = identify(adb, ip)
    if info["state"] != "device":
        raise InstallError(f"adb_{info['state']}")
    if info["serial_hash"] != serial_hash:
        raise InstallError("panel_changed")  # someone else took this IP since the scan
    step("found")
    rc, out = adb("install", "-r", "-g", str(apk), serial=target, timeout=180)
    if rc != 0 or "Success" not in out:
        m = re.search(r"Failure \[(\w+)", out)
        raise InstallError(m.group(1).lower() if m else "install_failed")
    step("installed")
    _, out = adb("shell", f"dpm set-device-owner {APP_ADMIN}", serial=target)
    mode = "device_owner" if "Success: Device owner set" in out else "launcher"
    if mode == "launcher":
        adb("shell", f"cmd package set-home-activity {APP_MAIN}", serial=target)
    step("kiosk")
    token = mint_token()
    adb("shell", f"am start -n {APP_MAIN} --es hub {hub_url} --es hub_id {hub_id} --es pair {token}", serial=target)
    deadline = time.monotonic() + PAIR_WAIT_S
    while not paired():
        if time.monotonic() > deadline:
            raise InstallError("pair_timeout")
        sleep(PAIR_POLL_S)
    step("paired")
    closed = close_adb(adb, ip, probe=probe, sleep=sleep)
    step("adb_closed" if closed else "adb_open")
    return {**info, "mode": mode, "adb_closed": closed}


def close_adb(adb, ip: str, probe=None, sleep=None) -> bool:
    """Switch adbd off and prove it: the port must stop answering (§7.4)."""
    probe = probe or tcp_open
    sleep = sleep or time.sleep
    target = f"{ip}:{ADB_PORT}"
    adb("shell", "settings put global adb_enabled 0", serial=target, timeout=10)
    adb("disconnect", target, timeout=10)
    for _ in range(CLOSE_WAIT_S):
        if not probe(ip, ADB_PORT):
            return True
        sleep(1)
    return False


def blink(adb, ip: str) -> None:
    """«Αναγνώριση»: the chosen panel flashes three times, so two identical panels can't be mixed up."""
    target = f"{ip}:{ADB_PORT}"
    _, cur = adb("shell", "settings get system screen_brightness", serial=target)
    for level in ("0", "255", "0", "255", "0", "255"):
        adb("shell", f"settings put system screen_brightness {level}", serial=target)
        time.sleep(0.35)
    if cur.strip().isdigit():
        adb("shell", f"settings put system screen_brightness {cur.strip()}", serial=target)
