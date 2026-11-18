#!/usr/bin/env python3
"""Get a YouTube refresh token for your channel, once, on your own computer (Windows, macOS or Linux).

1. Google Cloud Console > APIs & Services > Credentials > Create credentials > OAuth client ID > "Desktop app".
2. Run:  python3 scripts/get_youtube_token.py <CLIENT_ID> <CLIENT_SECRET>
3. A browser opens; sign in with the Google account that owns the channel and allow access.
4. Copy the printed YOUTUBE_REFRESH_TOKEN (and the channel id) into .env.

Uses only the Python standard library. The token is printed to your terminal only.
"""
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    cid, secret = sys.argv[1], sys.argv[2]
    state = secrets.token_urlsafe(16)
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state and "code" in q:
                got["code"] = q["code"][0]
                msg = "Done. You can close this tab and go back to the terminal."
            else:
                msg = "Something went wrong (no code). Close this tab and try again."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}/"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state})
    threading.Thread(target=srv.handle_request, daemon=True).start()
    print("Opening your browser. If it does not open, visit:\n" + url + "\n")
    webbrowser.open(url)
    while "code" not in got:
        threading.Event().wait(0.5)
    data = urllib.parse.urlencode({"code": got["code"], "client_id": cid, "client_secret": secret,
                                   "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    tok = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=20))
    if "refresh_token" not in tok:
        sys.exit("Google returned no refresh token. Remove the app's access at myaccount.google.com/permissions and retry.")
    req = urllib.request.Request("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                                 headers={"Authorization": "Bearer " + tok["access_token"]})
    ch = json.load(urllib.request.urlopen(req, timeout=20)).get("items", [])
    print("Put these in .env:\n")
    print(f"YOUTUBE_CLIENT_ID={cid}\nYOUTUBE_CLIENT_SECRET={secret}\nYOUTUBE_REFRESH_TOKEN={tok['refresh_token']}")
    if ch:
        print(f"YOUTUBE_CHANNEL_ID={ch[0]['id']}     # {ch[0]['snippet']['title']}")


if __name__ == "__main__":
    main()
