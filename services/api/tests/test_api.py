import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="transcode-test-")
os.environ["COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app

init_db()


def login(client: TestClient, email: str, password: str = "longenough") -> str:
    created = client.post("/api/signup", json={"email": email, "password": password})
    assert created.status_code == 201, created.text
    logged = client.post("/api/login", json={"email": email, "password": password})
    assert logged.status_code == 200, logged.text
    return logged.json()["csrf"]


def test_health_and_signup_rules():
    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.post("/api/signup", json={"email": "bad", "password": "longenough"}).status_code == 422
    assert client.post("/api/signup", json={"email": "a@b.co", "password": "short"}).status_code == 422
    assert client.post("/api/signup", json={"email": "dup@b.com", "password": "longenough"}).status_code == 201
    assert client.post("/api/signup", json={"email": "dup@b.com", "password": "longenough"}).status_code == 409
    assert client.post("/api/login", json={"email": "dup@b.com", "password": "wrong-pass"}).status_code == 401


def test_refresh_stays_logged_in_until_logout():
    client = TestClient(app)
    csrf = login(client, "session@b.com")
    assert client.get("/api/session").status_code == 200
    fresh = TestClient(app)
    fresh.cookies = client.cookies
    assert fresh.get("/api/session").json()["user"]["email"] == "session@b.com"
    assert client.post("/api/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/session").status_code == 401


def test_upload_is_private_and_idempotent():
    owner = TestClient(app)
    csrf = login(owner, "owner@b.com")
    stranger = TestClient(app)
    assert stranger.post(
        "/api/videos",
        headers={"Idempotency-Key": "x", "X-CSRF-Token": "nope"},
        json={"filename": "a.mp4", "content_type": "video/mp4", "size_bytes": 4},
    ).status_code == 401

    empty = owner.post(
        "/api/videos",
        headers={"Idempotency-Key": "empty", "X-CSRF-Token": csrf},
        json={"filename": "a.mp4", "content_type": "video/mp4", "size_bytes": 0},
    )
    assert empty.status_code == 422

    created = owner.post(
        "/api/videos",
        headers={"Idempotency-Key": "upload-1", "X-CSRF-Token": csrf},
        json={"filename": "clip.mp4", "content_type": "video/mp4", "size_bytes": 4},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job"]["id"]
    retry = owner.post(
        "/api/videos",
        headers={"Idempotency-Key": "upload-1", "X-CSRF-Token": csrf},
        json={"filename": "clip.mp4", "content_type": "video/mp4", "size_bytes": 4},
    )
    assert retry.json()["job"]["id"] == job_id
    assert owner.post(
        "/api/videos",
        headers={"Idempotency-Key": "upload-1", "X-CSRF-Token": csrf},
        json={"filename": "other.mp4", "content_type": "video/mp4", "size_bytes": 4},
    ).status_code == 409

    assert owner.post(f"/api/videos/{job_id}/complete", headers={"X-CSRF-Token": csrf}).status_code == 422
    put = owner.put(created.json()["upload_url"], content=b"1234")
    assert put.status_code == 204, put.text
    done = owner.post(f"/api/videos/{job_id}/complete", headers={"X-CSRF-Token": csrf})
    assert done.status_code == 202
    assert done.json()["status"] == "uploaded"
    assert owner.post(f"/api/videos/{job_id}/complete", headers={"X-CSRF-Token": csrf}).status_code == 202

    login(stranger, "other@b.com")
    assert stranger.get(f"/api/videos/{job_id}").status_code == 404
    assert owner.get(f"/api/videos/{job_id}").json()["status"] == "uploaded"
