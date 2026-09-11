import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    base_url: str
    admin_token: str
    encryption_key: str
    data_dir: Path = Path("data")
    interval_seconds: int = 300
    days_ahead: int = 90
    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant: str = "common"

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("BASE_URL must be an absolute HTTP(S) origin")
        if url.path or url.query or url.fragment or url.username or url.password:
            raise ValueError("BASE_URL must be an origin with no path or trailing slash")
        if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use HTTPS for non-local deployments")
        if len(self.admin_token) < 32:
            raise ValueError("ADMIN_TOKEN must contain at least 32 characters")
        Fernet(self.encryption_key.encode())
        if not 60 <= self.interval_seconds <= 86400 or not 1 <= self.days_ahead <= 365:
            raise ValueError("Use a 60–86400 second interval and a 1–365 day horizon")
        if not all(c.isalnum() or c in "-." for c in self.microsoft_tenant):
            raise ValueError("Invalid MICROSOFT_TENANT")

    @property
    def secure(self):
        return self.base_url.startswith("https:")

    def configured(self, provider):
        return bool(getattr(self, f"{provider}_client_id") and getattr(self, f"{provider}_client_secret"))

    @classmethod
    def from_env(cls):
        load_dotenv()
        return cls(
            base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
            admin_token=os.environ["ADMIN_TOKEN"],
            encryption_key=os.environ["ENCRYPTION_KEY"],
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            interval_seconds=int(os.getenv("SYNC_INTERVAL_SECONDS", "300")),
            days_ahead=int(os.getenv("SYNC_DAYS_AHEAD", "90")),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            microsoft_client_id=os.getenv("MICROSOFT_CLIENT_ID", ""),
            microsoft_client_secret=os.getenv("MICROSOFT_CLIENT_SECRET", ""),
            microsoft_tenant=os.getenv("MICROSOFT_TENANT", "common"),
        )
