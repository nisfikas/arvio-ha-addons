"""Software-only home occupancy — pure fusion. Mirrors packages/domain/src/occupancy.ts."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

HOME_OCCUPIED = "binary_sensor.arvio_home_occupied"
HOME_CONFIDENCE = "sensor.arvio_home_occupancy_confidence"
HOME_ENTERED = "arvio_presence.home_entered"
HOME_LEFT = "arvio_presence.home_left"

T_HOME_MIN_S = 15
T_HOME_MAX_S = 3600
T_HOME_DEFAULT_S = 180
T_ROOM_DWELL_MIN_S = 15
T_ROOM_DWELL_MAX_S = 600
T_ROOM_DWELL_DEFAULT_S = 60
ROOM_HYSTERESIS = 0.2
ROOM_EVENT_THETA = 0.6
STICKY_IDLE_S = 20 * 60
INTERACTION_FRESH_S = 10 * 60

SITE_HUB_RE = re.compile(r"^[a-z][a-z0-9_]{2,64}$")
MEMBER_RE = re.compile(r"^mem_[a-z0-9]{8,64}$")
TRACKER_RE = re.compile(r"^(device_tracker|person)\.[a-z0-9_]+$")
ENTITY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")
AREA_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FORBIDDEN = frozenset({"mac", "rssi", "bssid", "ip", "hostname", "bytes", "dpi"})
PROFILE_KEYS = frozenset({"site_id", "hub_id", "rev", "t_home_s", "t_room_dwell_s", "members", "sticky", "adjacency"})
MEMBER_KEYS = frozenset({"member_id", "tracker_entity_ids", "stable_mac"})
STICKY_KEYS = frozenset({"entity_id", "area_id"})
ADJ_KEYS = frozenset({"from", "to"})


def _rec(raw) -> dict | None:
    return raw if isinstance(raw, dict) else None


def _assert_keys(obj: dict, allowed: frozenset[str]) -> None:
    for key in obj:
        lk = str(key).lower()
        if lk in FORBIDDEN:
            raise ValueError(f"forbidden_field:{key}")
        if str(key) not in allowed:
            raise ValueError(f"unknown_field:{key}")


def _int_in(v, lo: int, hi: int, fallback: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        n = fallback
    else:
        n = int(v)
    if n < lo or n > hi:
        raise ValueError("invalid_t")
    return n


def parse_profile(raw) -> dict:
    b = _rec(raw)
    if not b:
        raise ValueError("invalid_payload")
    _assert_keys(b, PROFILE_KEYS)
    site_id = str(b.get("site_id") or "").strip()
    hub_id = str(b.get("hub_id") or "").strip()
    if not SITE_HUB_RE.match(site_id) or not SITE_HUB_RE.match(hub_id):
        raise ValueError("invalid_ids")
    rev = b.get("rev")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        raise ValueError("invalid_rev")
    members = []
    seen = set()
    for row in b.get("members") or []:
        m = _rec(row)
        if not m:
            raise ValueError("invalid_member")
        _assert_keys(m, MEMBER_KEYS)
        member_id = str(m.get("member_id") or "").strip()
        if not MEMBER_RE.match(member_id):
            raise ValueError("invalid_member_id")
        if member_id in seen:
            raise ValueError("duplicate_member")
        seen.add(member_id)
        ids = []
        for item in m.get("tracker_entity_ids") or []:
            eid = str(item or "").strip()
            if not TRACKER_RE.match(eid):
                raise ValueError(f"invalid_tracker:{eid}")
            if eid not in ids:
                ids.append(eid)
        if len(ids) > 4:
            raise ValueError("invalid_tracker")
        members.append({"member_id": member_id, "tracker_entity_ids": ids, "stable_mac": m.get("stable_mac") is True})
    sticky = []
    for row in b.get("sticky") or []:
        s = _rec(row)
        if not s:
            raise ValueError("invalid_sticky")
        _assert_keys(s, STICKY_KEYS)
        entity_id = str(s.get("entity_id") or "").strip()
        area_id = str(s.get("area_id") or "").strip()
        if not ENTITY_RE.match(entity_id) or not AREA_RE.match(area_id):
            raise ValueError("invalid_sticky")
        sticky.append({"entity_id": entity_id, "area_id": area_id})
    adjacency = []
    for row in b.get("adjacency") or []:
        a = _rec(row)
        if not a:
            raise ValueError("invalid_adjacency")
        _assert_keys(a, ADJ_KEYS)
        frm = str(a.get("from") or "").strip()
        to = str(a.get("to") or "").strip()
        if not AREA_RE.match(frm) or not AREA_RE.match(to) or frm == to:
            raise ValueError("invalid_adjacency")
        adjacency.append({"from": frm, "to": to})
    return {
        "site_id": site_id,
        "hub_id": hub_id,
        "rev": rev,
        "t_home_s": _int_in(b.get("t_home_s"), T_HOME_MIN_S, T_HOME_MAX_S, T_HOME_DEFAULT_S),
        "t_room_dwell_s": _int_in(b.get("t_room_dwell_s"), T_ROOM_DWELL_MIN_S, T_ROOM_DWELL_MAX_S, T_ROOM_DWELL_DEFAULT_S),
        "members": members,
        "sticky": sticky,
        "adjacency": adjacency,
    }


def bound_trackers(profile: dict) -> list[str]:
    ids: list[str] = []
    for m in profile.get("members") or []:
        if not m.get("stable_mac"):
            continue
        for eid in m.get("tracker_entity_ids") or []:
            if eid not in ids:
                ids.append(eid)
    return ids


def tracker_is_home(state: str | None) -> bool | None:
    s = (state or "").lower()
    if not s or s in ("unavailable", "unknown"):
        return None
    if s in ("home", "on"):
        return True
    return False


def last_changed_ms(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def tracker_from_ha(state_obj: dict) -> dict | None:
    if not isinstance(state_obj, dict):
        return None
    eid = str(state_obj.get("entity_id") or "")
    if not TRACKER_RE.match(eid):
        return None
    return {
        "entity_id": eid,
        "state": str(state_obj.get("state") or ""),
        "last_changed_ms": last_changed_ms(state_obj.get("last_changed")),
    }


def sanitize_source(state_obj: dict) -> dict | None:
    """Partner commissioning list — never MAC / IP / BSSID."""
    row = tracker_from_ha(state_obj)
    if not row:
        return None
    attrs = state_obj.get("attributes") if isinstance(state_obj.get("attributes"), dict) else {}
    source_type = attrs.get("source_type")
    return {
        "entity_id": row["entity_id"],
        "name": str(attrs.get("friendly_name") or row["entity_id"]),
        "state": row["state"],
        "source_type": str(source_type) if source_type not in (None, "") else None,
    }


def fuse_home_occupancy(profile: dict, trackers: list[dict], prev: dict | None, now_ms: int) -> dict:
    prev = prev if isinstance(prev, dict) else {}
    prev_phase = str(prev.get("phase") or "unknown")
    if prev_phase not in ("unknown", "home", "leaving", "away"):
        prev_phase = "unknown"
    try:
        prev_since = int(prev.get("since_ms") or now_ms)
    except (TypeError, ValueError):
        prev_since = now_ms
    by_id = {t["entity_id"]: t for t in trackers if isinstance(t, dict) and t.get("entity_id")}
    issues: list[str] = []
    home = away = unknown = bound = 0
    for member in profile.get("members") or []:
        ids = member.get("tracker_entity_ids") or []
        if not ids:
            continue
        bound += len(ids)
        if not member.get("stable_mac"):
            for eid in ids:
                issues.append(f"private_mac:{eid}")
            continue
        for eid in ids:
            row = by_id.get(eid)
            bit = tracker_is_home((row or {}).get("state") if row else "unavailable")
            if bit is True:
                home += 1
            elif bit is False:
                away += 1
            else:
                unknown += 1
    if bound == 0:
        return {
            "phase": "unknown",
            "occupied": None,
            "confidence": 0.15,
            "since_ms": prev_since if prev_phase == "unknown" else now_ms,
            "issues": [*issues, "no_bindings"],
        }
    usable = home + away
    phase = prev_phase
    since = prev_since
    event = None
    if home > 0:
        if prev_phase in ("away", "unknown"):
            event = "home_entered"
        phase = "home"
        if prev_phase != "home":
            since = now_ms
    elif usable == 0:
        phase = prev_phase
        since = prev_since
    elif prev_phase == "unknown":
        phase = "unknown"
        since = prev_since
    elif prev_phase == "home":
        phase = "leaving"
        since = now_ms
    elif prev_phase == "leaving":
        if now_ms - prev_since >= int(profile["t_home_s"]) * 1000:
            phase = "away"
            since = now_ms
            event = "home_left"
        else:
            phase = "leaving"
    else:
        phase = "away"
    occupied = True if phase in ("home", "leaving") else False if phase == "away" else None
    confidence = 0.2
    if phase == "home":
        confidence = 0.85 if away > 0 else 0.92
    elif phase == "leaving":
        confidence = 0.72
    elif phase == "away":
        confidence = 0.88
    if unknown > 0 and (usable + unknown) > 0:
        confidence = max(0.15, confidence - 0.15 * (unknown / (usable + unknown)))
    if any(i.startswith("private_mac:") for i in issues):
        confidence = min(confidence, 0.55)
    out = {
        "phase": phase,
        "occupied": occupied,
        "confidence": round(confidence, 2),
        "since_ms": since,
        "issues": issues,
    }
    if event:
        out["event"] = event
    return out


def publish_state(snap: dict) -> dict:
    occ = snap.get("occupied")
    return {
        "occupied_state": "on" if occ is True else "off" if occ is False else "unknown",
        "confidence_pct": int(round(float(snap.get("confidence") or 0) * 100)),
        "phase": snap.get("phase") or "unknown",
    }


def _clamp_p(p: float) -> float:
    return min(1 - 1e-6, max(1e-6, p))


def _log_odds(p: float) -> float:
    q = _clamp_p(p)
    return math.log(q / (1 - q))


def _softmax(logits: list[float]) -> list[float]:
    m = max(logits)
    ex = [math.exp(x - m) for x in logits]
    s = sum(ex) or 1.0
    return [x / s for x in ex]


def _adjacent(edges: list[dict], a: str, b: str) -> bool:
    if a == b:
        return True
    for e in edges:
        if (e.get("from") == a and e.get("to") == b) or (e.get("from") == b and e.get("to") == a):
            return True
    return False


def fuse_anonymous_rooms(profile: dict, evidence: list[dict], now_ms: int) -> list[dict]:
    by_area: dict[str, dict] = {}
    for s in profile.get("sticky") or []:
        by_area[s["area_id"]] = {"on": False, "age_ms": float("inf")}
    for ev in evidence:
        if ev.get("kind") != "sticky":
            continue
        age = max(0, now_ms - int(ev.get("last_changed_ms") or 0))
        on = bool(ev.get("active")) and age <= STICKY_IDLE_S * 1000
        area = str(ev.get("area_id") or "")
        cur = by_area.get(area) or {"on": False, "age_ms": float("inf")}
        if on and age <= cur["age_ms"]:
            by_area[area] = {"on": True, "age_ms": age}
        elif area not in by_area:
            by_area[area] = cur
    out = []
    for area_id, row in by_area.items():
        conf = 0.9 - (row["age_ms"] / (STICKY_IDLE_S * 1000)) * 0.4 if row["on"] else 0.2
        out.append({"area_id": area_id, "occupied": row["on"], "confidence": max(0.4, conf) if row["on"] else 0.2})
    return out


def fuse_person_room(profile: dict, member_id: str, areas: list[str], evidence: list[dict], prev: dict | None, now_ms: int) -> dict:
    labels = [a for a in dict.fromkeys(areas) if AREA_RE.match(a)] + ["unknown"]
    logits = [_log_odds(1 / len(labels)) for _ in labels]
    prev = prev if isinstance(prev, dict) else {}
    prev_area = prev.get("area_id")
    prev_p = float(prev.get("p") or 0)
    try:
        prev_since = int(prev.get("since_ms") or now_ms)
    except (TypeError, ValueError):
        prev_since = now_ms
    if prev_area in labels:
        age_s = max(0, (now_ms - prev_since) / 1000)
        decay = math.exp(-age_s / (30 * 60))
        idx = labels.index(prev_area)
        logits[idx] += _log_odds(0.5 + 0.3 * decay) - _log_odds(0.5)
    for ev in evidence:
        if not ev.get("active"):
            continue
        area = str(ev.get("area_id") or "")
        if area not in labels:
            continue
        age_s = max(0, (now_ms - int(ev.get("last_changed_ms") or 0)) / 1000)
        kind = ev.get("kind")
        if kind == "sticky":
            continue
        if kind == "ap":
            logits[labels.index(area)] += _log_odds(0.65) - _log_odds(0.35)
        if kind == "interaction" and age_s <= INTERACTION_FRESH_S:
            logits[labels.index(area)] += _log_odds(0.85) - _log_odds(0.15)
    has_identity = any(ev.get("active") and ev.get("kind") in ("ap", "interaction") for ev in evidence)
    if not has_identity:
        logits[labels.index("unknown")] += _log_odds(0.72) - _log_odds(0.28)
    ps = _softmax(logits)
    ranked = sorted(zip(labels, ps), key=lambda x: x[1], reverse=True)
    map_id, p = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    area_id = None if map_id == "unknown" else map_id
    event = None
    current = prev_area if prev_area else None
    if area_id and current and area_id != current:
        dwell_ok = now_ms - prev_since >= int(profile["t_room_dwell_s"]) * 1000
        hyst_ok = p >= prev_p + ROOM_HYSTERESIS
        adj_ok = not profile.get("adjacency") or _adjacent(profile["adjacency"], current, area_id)
        if not dwell_ok or not hyst_ok or not adj_ok:
            area_id = current
            p = prev_p
        elif p >= ROOM_EVENT_THETA:
            event = "room_entered"
    elif area_id and not current and p >= ROOM_EVENT_THETA:
        if now_ms - prev_since >= int(profile["t_room_dwell_s"]) * 1000:
            event = "room_entered"
        else:
            area_id = None
            p = prev_p
    since_ms = prev_since if area_id == current else now_ms
    out = {
        "member_id": member_id,
        "area_id": area_id,
        "p": round(p, 2),
        "confidence": max(0.0, round(p - second, 2)),
        "since_ms": since_ms,
    }
    if event:
        out["event"] = event
    return out
