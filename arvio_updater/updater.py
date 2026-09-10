#!/usr/bin/env python3
"""Apply Arvio Agent updates requested via /share/arvio/update_request.json.

Supervisor rejects add-on self-update (403). This companion calls the
documented store update endpoint for the Agent slug.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

SHARE = Path("/share/arvio")
REQUEST = SHARE / "update_request.json"
RESULT = SHARE / "update_result.json"
POLL_S = 8


def token() -> str:
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


def supervisor(path: str, method: str = "GET", body: dict | None = None, timeout: int = 300):
    tok = token()
    if not tok:
        raise RuntimeError("missing SUPERVISOR_TOKEN")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://supervisor{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def write_result(payload: dict) -> None:
    SHARE.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(payload, indent=2))


def process_request(req: dict) -> None:
    slug = str(req.get("slug") or "").strip()
    if not slug or slug in ("self", "local_arvio_updater", "arvio_updater"):
        # Never update ourselves from this file without an explicit agent slug.
        if not slug:
            raise RuntimeError("slug required")
    # Prefer agent slug from request; fall back common names.
    candidates = [slug]
    for alt in ("local_arvio_agent", "arvio_agent"):
        if alt not in candidates:
            candidates.append(alt)

    supervisor("/store/reload", "POST", {}, timeout=90)
    last_err = None
    for candidate in candidates:
        if "updater" in candidate:
            continue
        try:
            # Documented: POST /store/addons/{slug}/update
            resp = supervisor(
                f"/store/addons/{candidate}/update",
                "POST",
                {"backup": True, "background": True},
                timeout=30,
            )
            write_result(
                {
                    "ok": True,
                    "slug": candidate,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "response": resp,
                    "request": req,
                }
            )
            REQUEST.unlink(missing_ok=True)
            print(f"arvio_updater: update started for {candidate}", flush=True)
            return
        except Exception as e:
            last_err = e
            print(f"arvio_updater: {candidate} failed: {e}", flush=True)
    raise RuntimeError(str(last_err) or "update failed")


def main() -> None:
    print("arvio_updater: watching /share/arvio/update_request.json", flush=True)
    while True:
        try:
            if REQUEST.is_file():
                raw = REQUEST.read_text(encoding="utf-8")
                req = json.loads(raw) if raw else {}
                if isinstance(req, dict) and req.get("slug"):
                    process_request(req)
        except Exception as e:
            write_result(
                {
                    "ok": False,
                    "error": str(e),
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
            print(f"arvio_updater: error {e}", flush=True)
            # Avoid tight loop on persistent bad request
            try:
                REQUEST.unlink(missing_ok=True)
            except OSError:
                pass
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
