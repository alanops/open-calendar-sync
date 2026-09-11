import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from cryptography.fernet import Fernet


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, settings):
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = settings.data_dir / "calendar-sync.sqlite3"
        self.fernet = Fernet(settings.encryption_key.encode())
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, subject TEXT NOT NULL,
                    email TEXT NOT NULL, token TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(provider, subject)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth (
                    state TEXT PRIMARY KEY, session_id TEXT NOT NULL, provider TEXT NOT NULL,
                    verifier TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS copies (
                    key TEXT PRIMARY KEY, target TEXT NOT NULL, remote_id TEXT,
                    transaction_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL,
                    message TEXT NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('installation', ?)", (uuid.uuid4().hex,))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('paused', 'true')")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.connect() as db:
            return db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0]

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    def accounts(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM accounts ORDER BY provider, email")]

    def token(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT token FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise ValueError("Account not found")
        return json.loads(self.fernet.decrypt(row[0].encode()))

    def save_token(self, account_id, token):
        encrypted = self.fernet.encrypt(json.dumps(token).encode()).decode()
        with self.connect() as db:
            db.execute("UPDATE accounts SET token=? WHERE id=?", (encrypted, account_id))

    def add_account(self, provider, subject, email, token):
        account_id = digest(f"{provider}:{subject}")[:32]
        with self.connect() as db:
            previous = db.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
        if previous and not token.get("refresh_token"):
            token["refresh_token"] = self.token(account_id).get("refresh_token")
        if not token.get("refresh_token"):
            raise ValueError("Offline access was not granted. Reconnect and grant calendar access.")
        encrypted = self.fernet.encrypt(json.dumps(token).encode()).decode()
        with self.connect() as db:
            db.execute(
                """INSERT INTO accounts (id,provider,subject,email,token) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET email=excluded.email, token=excluded.token""",
                (account_id, provider, subject, email, encrypted),
            )
        return account_id

    def enable(self, account_id, enabled):
        with self.connect() as db:
            return db.execute("UPDATE accounts SET enabled=? WHERE id=?", (enabled, account_id)).rowcount

    def remove_account(self, account_id):
        with self.connect() as db:
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def new_session(self):
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(session), csrf, time.time() + 43200))
        return session, csrf

    def session(self, session):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id=? AND expires>?", (digest(session), time.time())
            ).fetchone()
            return dict(row) if row else None

    def logout(self, session):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (digest(session),))

    def oauth_start(self, session_id, provider, verifier):
        state = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM oauth WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO oauth VALUES (?,?,?,?,?)",
                (digest(state), session_id, provider, verifier, time.time() + 600),
            )
        return state

    def oauth_consume(self, state, session_id, provider):
        with self.connect() as db:
            row = db.execute(
                "DELETE FROM oauth WHERE state=? AND session_id=? AND provider=? "
                "AND expires>? RETURNING verifier",
                (digest(state), session_id, provider, time.time()),
            ).fetchone()
        return row[0] if row else None

    def copies(self):
        with self.connect() as db:
            return {r["key"]: dict(r) for r in db.execute("SELECT * FROM copies")}

    def intent(self, key, target):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO copies VALUES (?,?,NULL,?)", (key, target, uuid.uuid4().hex))
            return dict(db.execute("SELECT * FROM copies WHERE key=?", (key,)).fetchone())

    def remember(self, key, remote_id):
        with self.connect() as db:
            db.execute("UPDATE copies SET remote_id=? WHERE key=?", (remote_id, key))

    def forget(self, key):
        with self.connect() as db:
            db.execute("DELETE FROM copies WHERE key=?", (key,))

    def record(self, status, message):
        with self.connect() as db:
            db.execute("INSERT INTO runs (at,status,message) VALUES (?,?,?)", (time.time(), status, message))
            db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 50)")

    def runs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 12")]
