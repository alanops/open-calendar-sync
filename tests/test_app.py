import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from calendar_sync.app import create_app


@pytest.fixture
def client(settings):
    app = create_app(settings, background=False)
    with TestClient(app, base_url=settings.base_url) as client:
        yield client


def login(client, settings):
    response = client.post(
        "/api/login", json={"token": settings.admin_token}, headers={"Origin": settings.base_url}
    )
    assert response.status_code == 200
    return {"Origin": settings.base_url, "X-CSRF-Token": response.json()["csrf"]}


def test_auth_csrf_and_host_required(client, settings):
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/login", json={"token": settings.admin_token}).status_code == 403
    headers = login(client, settings)
    assert client.get("/api/status").status_code == 200
    assert (
        client.post("/api/pause", json={"paused": True}, headers={"Origin": settings.base_url}).status_code
        == 403
    )
    assert client.post("/api/pause", json={"paused": True}, headers=headers).status_code == 200
    assert client.get("/api/status", headers={"Host": "attacker.example"}).status_code == 400


def test_rate_limit_after_five_bad_keys(client, settings):
    for _ in range(5):
        assert (
            client.post(
                "/api/login", json={"token": "bad"}, headers={"Origin": settings.base_url}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/login", json={"token": settings.admin_token}, headers={"Origin": settings.base_url}
        ).status_code
        == 429
    )


def test_logout_invalidates_session(client, settings):
    headers = login(client, settings)
    old_cookie = client.cookies.get("ocs_session")
    assert client.post("/api/logout", json={}, headers=headers).status_code == 200
    client.cookies.set("ocs_session", old_cookie)
    assert client.get("/api/status").status_code == 401


def test_status_never_exposes_secrets(client, settings, token):
    login(client, settings)
    store = client.app.state.store
    store.add_account("google", "subject", "person@example.com", token)
    result = client.get("/api/status")
    assert "test-refresh" not in result.text and "test-access" not in result.text
    assert "client_secret" not in result.text
    raw = store.path.read_bytes()
    assert b"test-refresh" not in raw and b"test-access" not in raw


def test_oauth_pkce_state_is_single_use_and_bound_to_session_and_provider(client, settings):
    headers = login(client, settings)
    response = client.post("/api/connect/google", json={}, headers=headers)
    query = parse_qs(urlsplit(response.json()["url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [settings.base_url + "/oauth/google/callback"]
    assert query["access_type"] == ["offline"]
    assert query["scope"] == ["openid email https://www.googleapis.com/auth/calendar.events"]
    state = query["state"][0]
    store = client.app.state.store
    session = store.session(client.cookies["ocs_session"])
    assert store.oauth_consume(state, "other-session", "google") is None
    assert store.oauth_consume(state, session["id"], "microsoft") is None
    verifier = store.oauth_consume(state, session["id"], "google")
    assert verifier and len(verifier) >= 43
    import base64

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected]
    assert store.oauth_consume(state, session["id"], "google") is None


def test_declined_oauth_does_not_add_account(client, settings):
    headers = login(client, settings)
    url = client.post("/api/connect/google", json={}, headers=headers).json()["url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    response = client.get(
        "/oauth/google/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.app.state.store.accounts() == []
    assert (
        client.get("/oauth/google/callback", params={"state": state, "error": "access_denied"}).status_code
        == 400
    )


def test_connection_and_configuration_changes_start_paused(client, settings, token):
    headers = login(client, settings)
    account_id = client.app.state.store.add_account("google", "subject", "person@example.com", token)
    client.app.state.store.set("paused", "false")
    assert (
        client.post(
            f"/api/accounts/{account_id}/enabled", json={"enabled": True}, headers=headers
        ).status_code
        == 200
    )
    assert client.get("/api/status").json()["paused"] is True
    assert client.post("/api/pause", json={"paused": False}, headers=headers).status_code == 400


def test_dashboard_assets_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200 and "Open Calendar Sync" in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
