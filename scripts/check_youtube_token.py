#!/usr/bin/env python3
"""Check the YouTube OAuth settings in your .env: refreshes an access token and prints its scopes and channel.

    python3 scripts/check_youtube_token.py            (reads ./.env)
Needs only the Python standard library. Prints no secrets.
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def load_env(path: Path) -> dict:
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return out


def main():
    env = load_env(Path(sys.argv[1] if len(sys.argv) > 1 else ".env"))
    need = ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")
    data = urllib.parse.urlencode({"client_id": env["YOUTUBE_CLIENT_ID"], "client_secret": env["YOUTUBE_CLIENT_SECRET"],
                                   "refresh_token": env["YOUTUBE_REFRESH_TOKEN"], "grant_type": "refresh_token"}).encode()
    try:
        tok = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=20))
    except urllib.error.HTTPError as e:
        sys.exit(f"Token refresh failed ({e.code}): {e.read()[:200].decode(errors='replace')}\n"
                 "unauthorized_client = this refresh token was issued for a different OAuth client ID.")
    print("Scopes:", tok.get("scope"))
    for s in ("youtube.upload", "youtube.readonly"):
        if s not in tok.get("scope", ""):
            print(f"  WARNING: scope {s} is missing")
    req = urllib.request.Request("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                                 headers={"Authorization": "Bearer " + tok["access_token"]})
    ch = json.load(urllib.request.urlopen(req, timeout=20))
    for item in ch.get("items", []):
        print(f"Channel: {item['snippet']['title']}  ({item['id']})")
        if env.get("YOUTUBE_CHANNEL_ID") and env["YOUTUBE_CHANNEL_ID"] != item["id"]:
            print("  WARNING: YOUTUBE_CHANNEL_ID in .env is a different channel")
    if not ch.get("items"):
        print("This token has no YouTube channel.")


if __name__ == "__main__":
    main()
