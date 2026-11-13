"""Guest TV: the profile a TV in a rented room shows, the stay, and what GET /api/tv answers.

Plan and contract: docs/plans/2026-11-07-guest-tv.md in the monorepo (§2, §3.3). The checks mirror
packages/domain/src/guest-tv.ts; tests/test_guest_tv.py runs the same samples as its tests.
Pure: agent.py keeps the files, the keys and the HTTP.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

BLOCK_KINDS = (
    "logo", "welcome", "stay", "wifi", "guest_pass", "sections", "rules",
    "contacts", "clock", "weather", "link", "text", "apps",
)
SINGLE_KINDS = frozenset({"logo", "stay", "wifi", "guest_pass", "clock", "weather", "apps"})
APP_IDS = ("live_tv", "hdmi", "netflix", "youtube", "prime_video", "disney_plus", "spotify", "plex", "browser")
THEME_PRESETS = frozenset({"afisa", "grafitis", "penteli", "drys", "lino", "beton", "aigaio", "elia", "vasaltis", "galini", "lykofos"})
BLOCKS_MAX = 16
ITEMS_MAX = 8
RULES_MAX = 12
CONTACTS_MAX = 4
TITLE_MAX = 60
TEXT_MAX = 800
URL_MAX = 500
JSON_MAX = 40_000
INK_CONTRAST_MIN = 4.5
ACCENT_CONTRAST_MIN = 3.0
LOGO_B64_MAX = 280_000
WIFI_MAX = 64

PHONE_RE = re.compile(r"^\+?[0-9 ()-]{3,30}$")
HEX_RE = re.compile(r"^#[0-9a-f]{6}$")
STAY_ID_RE = re.compile(r"^sty_[A-Za-z0-9_-]{6,40}$")
SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
STAY_SPAN_MAX_S = 400 * 24 * 3600


def _str(raw, max_len: int, code: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(code)
    v = raw.strip()
    if len(v) > max_len:
        raise ValueError(code)
    return v or None


def _text(raw, max_len: int, code: str, required: bool = False) -> dict:
    """A plain string is Greek (what «Καλωσόρισμα» holds); {el, en} otherwise."""
    if raw is None or isinstance(raw, str):
        t = {"el": _str(raw, max_len, code), "en": None}
    elif isinstance(raw, dict):
        t = {"el": _str(raw.get("el"), max_len, code), "en": _str(raw.get("en"), max_len, code)}
    else:
        raise ValueError(code)
    if required and not t["el"] and not t["en"]:
        raise ValueError(code)
    return t


def _list(raw, max_len: int, code: str) -> list:
    if not isinstance(raw, list) or not raw or len(raw) > max_len:
        raise ValueError(code)
    return raw


def _https(raw, code: str) -> str:
    if not isinstance(raw, str) or len(raw) > URL_MAX:
        raise ValueError(code)
    u = urlparse(raw.strip())
    if u.scheme != "https" or not u.hostname:
        raise ValueError(code)
    return u.geturl()


def _block(raw) -> dict:
    if not isinstance(raw, dict) or raw.get("kind") not in BLOCK_KINDS:
        raise ValueError("invalid_block")
    kind = raw["kind"]
    if kind == "welcome":
        return {
            "kind": kind,
            "title": _text(raw.get("title"), TITLE_MAX, "invalid_welcome", True),
            "text": _text(raw.get("text"), TEXT_MAX, "invalid_welcome"),
        }
    if kind == "sections":
        items = []
        for s in _list(raw.get("items"), ITEMS_MAX, "invalid_sections"):
            if not isinstance(s, dict):
                raise ValueError("invalid_sections")
            items.append({
                "title": _text(s.get("title"), TITLE_MAX, "invalid_sections", True),
                "text": _text(s.get("text"), TEXT_MAX, "invalid_sections", True),
            })
        return {"kind": kind, "items": items}
    if kind == "rules":
        return {"kind": kind, "items": [_text(r, 200, "invalid_rules", True) for r in _list(raw.get("items"), RULES_MAX, "invalid_rules")]}
    if kind == "contacts":
        items = []
        for c in _list(raw.get("items"), CONTACTS_MAX, "invalid_contacts"):
            if not isinstance(c, dict):
                raise ValueError("invalid_contacts")
            name = _str(c.get("name"), TITLE_MAX, "invalid_contacts")
            phone = _str(c.get("phone"), 30, "invalid_contacts")
            if not name or not phone or not PHONE_RE.match(phone):
                raise ValueError("invalid_contacts")
            items.append({"name": name, "phone": phone, "role": _text(c.get("role"), TITLE_MAX, "invalid_contacts")})
        return {"kind": kind, "items": items}
    if kind == "link":
        return {"kind": kind, "url": _https(raw.get("url"), "invalid_link"), "caption": _text(raw.get("caption"), TITLE_MAX, "invalid_link", True)}
    if kind == "text":
        return {"kind": kind, "text": _text(raw.get("text"), TEXT_MAX, "invalid_text", True)}
    return {"kind": kind}


def contrast_ratio(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, bl = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _theme(raw) -> dict:
    o = raw if isinstance(raw, dict) else {}
    preset = o.get("preset") or "afisa"
    if preset not in THEME_PRESETS:
        raise ValueError("invalid_theme")
    mode = o.get("mode") or "dark"
    if mode not in ("light", "dark"):
        raise ValueError("invalid_theme")
    colors = o.get("colors")
    if colors is None:
        return {"preset": preset, "mode": mode, "colors": None}
    if not isinstance(colors, dict):
        raise ValueError("invalid_theme")
    bg, ink, accent = (str(colors.get(k) or "").strip().lower() if isinstance(colors.get(k), str) else "" for k in ("bg", "ink", "accent"))
    if not all(HEX_RE.match(c) for c in (bg, ink, accent)):
        raise ValueError("invalid_theme")
    if contrast_ratio(ink, bg) < INK_CONTRAST_MIN or contrast_ratio(accent, bg) < ACCENT_CONTRAST_MIN:
        raise ValueError("low_contrast")
    return {"preset": preset, "mode": mode, "colors": {"bg": bg, "ink": ink, "accent": accent}}


def parse_guest_tv(raw) -> dict:
    """`home_welcome.info.tv` as the cloud sends it; ValueError(code) on anything a TV could not show."""
    if not isinstance(raw, dict):
        raise ValueError("invalid_tv")
    blocks_raw = raw.get("blocks")
    if not isinstance(blocks_raw, list) or len(blocks_raw) > BLOCKS_MAX:
        raise ValueError("invalid_blocks")
    blocks = [_block(b) for b in blocks_raw]
    seen: set[str] = set()
    for b in blocks:
        if b["kind"] in SINGLE_KINDS and b["kind"] in seen:
            raise ValueError("duplicate_block")
        seen.add(b["kind"])
    apps = raw.get("apps", [])
    if not isinstance(apps, list) or any(a not in APP_IDS for a in apps) or len(set(apps)) != len(apps):
        raise ValueError("invalid_apps")
    out = {"blocks": blocks, "theme": _theme(raw.get("theme")), "apps": list(apps)}
    if len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > JSON_MAX:
        raise ValueError("tv_too_large")
    return out


def _iso_s(raw) -> float:
    if not isinstance(raw, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T", raw):
        raise ValueError("invalid_stay")
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_stay") from None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_guest_stay(raw) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not STAY_ID_RE.match(raw["id"]):
        raise ValueError("invalid_stay")
    t_in, t_out = _iso_s(raw.get("check_in")), _iso_s(raw.get("check_out"))
    if t_out <= t_in or t_out - t_in > STAY_SPAN_MAX_S:
        raise ValueError("invalid_stay")
    language = raw.get("language") or "en"
    if language not in ("el", "en"):
        raise ValueError("invalid_stay")
    source = raw.get("source") or "manual"
    if not isinstance(source, str) or not SOURCE_RE.match(source):
        raise ValueError("invalid_stay")
    pass_url = raw.get("pass_url")
    return {
        "id": raw["id"],
        "guest_name": _str(raw.get("guest_name"), TITLE_MAX, "invalid_stay"),
        "check_in": _iso(t_in),
        "check_out": _iso(t_out),
        "language": language,
        "pass_url": None if pass_url is None else _https(pass_url, "invalid_stay"),
        "source": source,
    }


def parse_guest_logo(raw) -> dict | None:
    """The house's logo (as welcome.ts parseWelcomeLogo): PNG or JPEG, base64, ≤ LOGO_B64_MAX."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("mime") not in ("image/png", "image/jpeg") or not isinstance(raw.get("data_base64"), str):
        raise ValueError("invalid_logo")
    b64 = re.sub(r"\s+", "", raw["data_base64"])
    if len(b64) < 32 or len(b64) > LOGO_B64_MAX:
        raise ValueError("invalid_logo")
    try:
        head = base64.b64decode(b64[:16], validate=True)
    except ValueError:
        raise ValueError("invalid_logo") from None
    magic = b"\x89PNG" if raw["mime"] == "image/png" else b"\xff\xd8\xff"
    if not head.startswith(magic):
        raise ValueError("invalid_logo")
    return {"mime": raw["mime"], "data_base64": b64}


def parse_put_guest_tv(payload) -> dict:
    """`arvio.put_guest_tv` from the cloud: the profile plus the Wi-Fi and the logo of «Καλωσόρισμα»."""
    p = payload if isinstance(payload, dict) else {}
    wifi_name = _str(p.get("wifi_name"), WIFI_MAX, "invalid_wifi")
    wifi_password = _str(p.get("wifi_password"), WIFI_MAX, "invalid_wifi")
    return {
        "tv": parse_guest_tv(p.get("tv")),
        "wifi": {"name": wifi_name, "password": wifi_password} if wifi_name else None,
        "logo": parse_guest_logo(p.get("logo")),
        "updated_at": str(p.get("updated_at") or ""),
    }


def tv_version(tv: dict) -> str:
    return hashlib.sha256(json.dumps(tv, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def tv_body(row: dict, doc: dict | None, *, have: str | None, now: str, tz: str, weather, hub: str) -> dict:
    """GET /api/tv (contract §3.3). `tv` only when the page does not have this version already."""
    doc = doc if isinstance(doc, dict) else {}
    profile = doc.get("tv") if isinstance(doc.get("tv"), dict) else {"blocks": [], "theme": _theme(None), "apps": []}
    try:
        stay = parse_guest_stay(row.get("stay"))
    except ValueError:
        stay = None
    tv = {
        "name": row.get("name"),
        "theme": profile.get("theme"),
        "languages": ["el", "en"],
        "blocks": profile.get("blocks") or [],
        "apps": profile.get("apps") or [],
        "logo": doc.get("logo"),
        "wifi": doc.get("wifi"),
        "stay": stay,
    }
    version = tv_version(tv)
    out = {"ok": True, "hub_id": hub, "now": now, "timezone": tz, "version": version, "weather": weather}
    if have == version:
        out["same"] = True
    else:
        out["tv"] = tv
    return out
