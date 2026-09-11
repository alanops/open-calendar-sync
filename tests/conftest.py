import time

import pytest
from cryptography.fernet import Fernet

from calendar_sync.config import Settings
from calendar_sync.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        base_url="http://localhost:8000",
        admin_token="test-only-key-" * 4,
        encryption_key=Fernet.generate_key().decode(),
        data_dir=tmp_path,
        google_client_id="test-google",
        google_client_secret="test-google-secret",
        microsoft_client_id="test-microsoft",
        microsoft_client_secret="test-microsoft-secret",
    )


@pytest.fixture
def store(settings):
    return Store(settings)


@pytest.fixture
def token():
    return {"access_token": "test-access", "refresh_token": "test-refresh", "expires_at": time.time() + 3600}
