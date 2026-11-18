"""API: auth, CSRF, roles, streams, clips pagination, Range previews, upload duplicate protection."""
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from clippingshorts import db, google_oauth, youtube_uploader
from clippingshorts.api.main import app
from clippingshorts.config import settings
from clippingshorts.jobs import JobError, create_upload

from .conftest import make_video

H = {"X-ClippingShorts": "1"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(c, pw="admin-password-123"):
    r = c.post("/api/auth/login", json={"password": pw})
    assert r.status_code == 200
    return r.json()["role"]


def test_auth_required_and_roles(client):
    client.cookies.clear()
    assert client.get("/api/streams").status_code == 401
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert login(client, "viewer-password-123") == "viewer"
    assert client.get("/api/streams").status_code == 200
    r = client.post("/api/streams", json={"streams": [{"url": "twitch.tv/x1", "platform": "twitch"}]}, headers=H)
    assert r.status_code == 403                          # viewer cannot change anything
    assert login(client) == "admin"
    r = client.post("/api/streams", json={"streams": [{"url": "twitch.tv/x1", "platform": "twitch"}]})
    assert r.status_code == 403 and "X-ClippingShorts" in r.text      # CSRF header missing


def test_streams_crud_and_control(client):
    login(client)
    body = {"streams": [{"url": "https://www.twitch.tv/api_a", "platform": "twitch", "creator_name": "A"},
                        {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "platform": "youtube"},
                        {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "platform": "youtube"},
                        {"url": "https://www.twitch.tv/api_b", "platform": "youtube"}]}
    r = client.post("/api/streams", json=body, headers=H)
    assert r.status_code == 201
    assert len(r.json()["created"]) == 2 and len(r.json()["errors"]) == 2
    sid = r.json()["created"][0]["id"]
    assert client.post(f"/api/streams/{sid}/start", headers=H).json()["desired_state"] == "running"
    assert client.get(f"/api/streams/{sid}/health").json()["desired_state"] == "running"
    assert client.post(f"/api/streams/{sid}/stop", headers=H).json()["desired_state"] == "stopped"
    r = client.patch(f"/api/streams/{sid}", json={"settings": {"local_trigger_score": 70}}, headers=H)
    assert r.json()["settings"]["local_trigger_score"] == 70
    assert client.post("/api/streams/start-all", headers=H).status_code == 200
    assert client.post("/api/streams/stop-all", headers=H).status_code == 200
    assert client.post("/api/streams", json={"streams": []}, headers=H).status_code == 422
    assert client.post("/api/streams", json={"streams": [{"url": "x", "platform": "twitch"}] * 11},
                       headers=H).status_code == 422


@pytest.fixture(scope="module")
def ready_clip():
    with db.session() as s:
        c = db.Clip(platform="twitch", creator_name="Range Test", status="ready", duration=10, width=1280,
                    height=720, title="Range clip", rel_dir="rangetest/c1")
        s.add(c)
    make_video(settings.source_dir / "rangetest/c1/source.mp4", seconds=10)
    return c


def test_clip_list_pagination_and_filters(client, ready_clip):
    login(client)
    r = client.get("/api/clips", params={"page_size": 1, "platform": "twitch"})
    assert r.status_code == 200 and r.json()["page_size"] == 1 and r.json()["total"] >= 1
    assert client.get("/api/clips", params={"platform": "youtube", "creator": "Range"}).json()["total"] == 0
    assert client.get(f"/api/clips/{ready_clip.id}").json()["title"] == "Range clip"


def test_preview_supports_range(client, ready_clip):
    login(client)
    full = client.get(f"/api/clips/{ready_clip.id}/preview")
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    size = len(full.content)
    r = client.get(f"/api/clips/{ready_clip.id}/preview", headers={"Range": "bytes=100-1099"})
    assert r.status_code == 206 and len(r.content) == 1000
    assert r.headers["content-range"] == f"bytes 100-1099/{size}"
    assert r.content == full.content[100:1100]
    r = client.get(f"/api/clips/{ready_clip.id}/preview", headers={"Range": "bytes=-500"})
    assert r.status_code == 206 and r.content == full.content[-500:]
    assert client.get(f"/api/clips/{ready_clip.id}/preview", headers={"Range": f"bytes={size + 10}-"}).status_code == 416


def test_edit_validation(client, ready_clip):
    login(client)
    bad = client.post(f"/api/clips/{ready_clip.id}/edit", json={"start": 5, "end": 5.2}, headers=H)
    assert bad.status_code == 422
    past = client.post(f"/api/clips/{ready_clip.id}/edit", json={"start": 0, "end": 99}, headers=H)
    assert past.status_code == 422
    ok = client.post(f"/api/clips/{ready_clip.id}/edit", json={"start": 1, "end": 6, "aspect": "1:1"}, headers=H)
    assert ok.status_code == 201 and ok.json()["version"] >= 1


# ---------------------------------------------------------------- uploads against a fake YouTube
class FakeYouTube(BaseHTTPRequestHandler):
    received = 0
    fail_once = True

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.send_response(200)
        self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/session")
        self.end_headers()

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        rng = self.headers.get("Content-Range", "")
        if rng.startswith("bytes */"):
            self.send_response(308)
            if FakeYouTube.received:
                self.send_header("Range", f"bytes=0-{FakeYouTube.received - 1}")
            self.end_headers()
            return
        if FakeYouTube.fail_once:
            FakeYouTube.fail_once = False
            self.send_response(503)
            self.end_headers()
            return
        FakeYouTube.received += len(body)
        total = int(rng.split("/")[1])
        if FakeYouTube.received >= total:
            out = b'{"id": "FAKEVIDEO01"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        else:
            self.send_response(308)
            self.send_header("Range", f"bytes=0-{FakeYouTube.received - 1}")
            self.end_headers()


def test_upload_resumes_and_is_never_duplicated(ready_clip, monkeypatch, tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), FakeYouTube)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(youtube_uploader, "UPLOAD_BASE", f"http://127.0.0.1:{srv.server_port}/upload")
    monkeypatch.setattr(youtube_uploader, "CHUNK", 64 * 1024)
    monkeypatch.setattr(youtube_uploader.time, "sleep", lambda s: None)
    monkeypatch.setattr(google_oauth, "headers", lambda: {"Authorization": "Bearer test"})
    with db.session() as s:
        e = db.Edit(clip_id=ready_clip.id, version=99, status="done", output_rel="rangetest/x.mp4")
        s.add(e)
    f = make_video(settings.edited_dir / "rangetest/x.mp4", seconds=6)
    with db.session() as s:
        up = db.Upload(edit_id=e.id, clip_id=ready_clip.id, channel_id="UCtest", title="t", privacy="private",
                       publish_at=time.time() + 3600, status="uploading")
        s.add(up)
    body = youtube_uploader.build_body(up)
    assert body["status"]["privacyStatus"] == "private" and "publishAt" in body["status"]
    prog = []
    vid = youtube_uploader.upload(up.id, f, prog.append)
    srv.shutdown()
    assert vid == "FAKEVIDEO01" and prog[-1] == 100
    db.update(db.Upload, up.id, status="done", youtube_video_id=vid)
    with pytest.raises(JobError):
        create_upload(db.get(db.Edit, e.id), "again", "", [], "public", channel_id="UCtest")


def test_dashboard_and_system(client):
    login(client)
    d = client.get("/api/dashboard").json()
    for k in ("streams_total", "clips_pending_review", "clips_uploaded", "resources", "health", "limits"):
        assert k in d
    assert client.get("/api/system/storage").json()["total_mb"] >= 0
    assert client.get("/api/system/usage").status_code == 200
    assert client.get("/api/jobs").status_code == 200
