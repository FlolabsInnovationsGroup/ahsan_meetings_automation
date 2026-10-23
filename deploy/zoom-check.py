#!/usr/bin/env python3
"""Check the Zoom Server-to-Server OAuth app in .env before using it.

Prints the scopes the app has and whether it can list cloud recordings
(account-wide and for the main user). Reads ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID
and ZOOM_CLIENT_SECRET from .env. Never prints the secret or the token.

    python3 deploy/zoom-check.py
"""

import base64
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def load_env(path=".env"):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def call(url, token=None, basic=None, method="GET"):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if basic:
        req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def main():
    env = load_env()
    missing = [k for k in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET") if not env.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")

    basic = base64.b64encode(f"{env['ZOOM_CLIENT_ID']}:{env['ZOOM_CLIENT_SECRET']}".encode()).decode()
    query = urllib.parse.urlencode({"grant_type": "account_credentials", "account_id": env["ZOOM_ACCOUNT_ID"]})
    status, data = call(f"https://zoom.us/oauth/token?{query}", basic=basic, method="POST")
    if "access_token" not in data:
        sys.exit(f"TOKEN FAILED ({status}): {data.get('reason') or data.get('error') or data}")
    token = data["access_token"]
    scopes = sorted((data.get("scope") or "").split())
    print("TOKEN OK")
    print("SCOPES:")
    for scope in scopes:
        print("  -", scope)

    since = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    for label, path in (("ACCOUNT recordings", "accounts/me/recordings"), ("USER (me) recordings", "users/me/recordings")):
        status, data = call(f"https://api.zoom.us/v2/{path}?from={since}&page_size=10", token=token)
        print(f"\n{label}: HTTP {status}")
        if status != 200:
            print("  ", data.get("message") or data)
            continue
        meetings = data.get("meetings", [])
        print(f"   {data.get('total_records', len(meetings))} meeting(s) with recordings in the last 30 days")
        for m in meetings[:10]:
            kinds = sorted({f.get("file_type") for f in m.get("recording_files", [])})
            print(f"   - {m.get('start_time', '')[:16]}  {m.get('topic', '')[:50]}  {kinds}")


if __name__ == "__main__":
    main()
