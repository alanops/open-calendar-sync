from dataclasses import replace

import pytest

from calendar_sync.models import Event


def test_reconnection_preserves_identity_and_refresh_token(store, token):
    account = store.add_account("google", "subject", "old@example.com", token)
    store.enable(account, True)
    row = store.intent("copy", account)
    new_token = {**token, "access_token": "replacement"}
    del new_token["refresh_token"]
    assert store.add_account("google", "subject", "new@example.com", new_token) == account
    assert store.token(account)["refresh_token"] == token["refresh_token"]
    assert store.accounts()[0]["enabled"] == 1
    assert store.copies()["copy"]["transaction_id"] == row["transaction_id"]


def test_missing_offline_access_not_saved(store):
    with pytest.raises(ValueError, match="Offline"):
        store.add_account("google", "subject", "owner@example.com", {"access_token": "test"})
    assert not store.accounts()


def test_oauth_state_expires(store):
    state = store.oauth_start("session", "google", "verifier")
    with store.connect() as db:
        db.execute("UPDATE oauth SET expires=0")
    assert store.oauth_consume(state, "session", "google") is None


def test_wrong_encryption_key_cannot_decrypt(settings, store, token):
    from cryptography.fernet import Fernet, InvalidToken

    from calendar_sync.store import Store

    account = store.add_account("google", "subject", "owner@example.com", token)
    different = Store(replace(settings, encryption_key=Fernet.generate_key().decode()))
    with pytest.raises(InvalidToken):
        different.token(account)


@pytest.mark.parametrize(
    "start,end,all_day",
    [
        ("2026-01-01T10:00:00", "2026-01-01T11:00:00", False),
        ("2026-01-01T11:00:00Z", "2026-01-01T10:00:00Z", False),
        ("2026-01-01", "2026-01-01", True),
    ],
)
def test_ambiguous_or_invalid_event_time_rejected(start, end, all_day):
    with pytest.raises(ValueError):
        Event("invalid", start, end, all_day=all_day)


def test_public_http_origin_rejected(settings):
    with pytest.raises(ValueError, match="HTTPS"):
        replace(settings, base_url="http://calendar.example.com")
