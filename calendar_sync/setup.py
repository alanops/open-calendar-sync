"""Generate private local configuration: python -m calendar_sync.setup."""

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


def main():
    path = Path(".env")
    contents = (
        "BASE_URL=http://localhost:8000\n"
        f"ADMIN_TOKEN={secrets.token_urlsafe(48)}\n"
        f"ENCRYPTION_KEY={Fernet.generate_key().decode()}\n"
        "DATA_DIR=data\nSYNC_INTERVAL_SECONDS=300\nSYNC_DAYS_AHEAD=90\n"
        "GOOGLE_CLIENT_ID=\nGOOGLE_CLIENT_SECRET=\n"
        "MICROSOFT_CLIENT_ID=\nMICROSOFT_CLIENT_SECRET=\nMICROSOFT_TENANT=common\n"
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(".env already exists; left unchanged.") from None
    with os.fdopen(fd, "w") as file:
        file.write(contents)
    print("Created .env. Use its ADMIN_TOKEN as your dashboard access key. Keep this file private.")
    print("Add your Google and Microsoft OAuth app credentials before connecting calendars.")


if __name__ == "__main__":
    main()
