import asyncio
import contextlib
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .engine import Engine
from .providers import ProviderError, exchange, oauth_client, oauth_endpoints
from .store import Store

STATIC = Path(__file__).parent / "static"


class Login(BaseModel):
    token: str = Field(max_length=512)


class Enabled(BaseModel):
    enabled: bool


class Paused(BaseModel):
    paused: bool


def create_app(settings=None, *, background=True):
    settings = settings or Settings.from_env()
    store = Store(settings)
    engine = Engine(settings, store)

    @asynccontextmanager
    async def lifespan(app):
        # One scheduler owns the database. Multiple worker processes are deliberately rejected.
        import fcntl

        with (settings.data_dir / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "This data directory already has a worker. Run exactly one process."
                ) from exc
            task = asyncio.create_task(engine.loop()) if background else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(
        title="Open Calendar Sync", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    from urllib.parse import urlsplit

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.base_url).hostname])
    app.state.store, app.state.engine = store, engine
    login_attempts = defaultdict(list)

    @app.middleware("http")
    async def security(request, call_next):
        if (
            request.headers.get("content-length", "").isdigit()
            and int(request.headers["content-length"]) > 8192
        ):
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != settings.base_url:
                return JSONResponse({"detail": "Request origin rejected"}, status_code=403)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            }
        )
        if settings.secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def authenticated(request: Request):
        session = store.session(request.cookies.get("ocs_session", ""))
        if not session:
            raise HTTPException(401, "Sign in to manage your calendars.")
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "Session check failed. Refresh and try again.")
        return session

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def home():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        # Bound memory and slow repeated attempts; client IP never comes from an untrusted header.
        now = time.time()
        for key in list(login_attempts):
            login_attempts[key] = [at for at in login_attempts[key] if at > now - 900]
            if not login_attempts[key]:
                del login_attempts[key]
        ip = request.client.host if request.client else "local"
        if len(login_attempts) >= 1000 or len(login_attempts[ip]) >= 5:
            raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
        if not secrets.compare_digest(body.token, settings.admin_token):
            login_attempts[ip].append(now)
            raise HTTPException(401, "Access key not recognised.")
        login_attempts.pop(ip, None)
        token, csrf = store.new_session()
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            "ocs_session",
            token,
            max_age=43200,
            httponly=True,
            secure=settings.secure,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request, session=Depends(authenticated)):
        store.logout(request.cookies["ocs_session"])
        response = JSONResponse({"ok": True})
        response.delete_cookie("ocs_session", path="/")
        return response

    @app.get("/api/status")
    async def status(session=Depends(authenticated)):
        return {
            "csrf": session["csrf"],
            "paused": store.get("paused") == "true",
            "running": engine.lock.locked(),
            "days_ahead": settings.days_ahead,
            "interval_seconds": settings.interval_seconds,
            "providers": {p: settings.configured(p) for p in ("google", "microsoft")},
            "accounts": [{k: a[k] for k in ("id", "provider", "email", "enabled")} for a in store.accounts()],
            "runs": store.runs(),
            "managed_copies": len(store.copies()),
        }

    @app.post("/api/connect/{provider}")
    async def connect(provider: str, session=Depends(authenticated)):
        if provider not in {"google", "microsoft"}:
            raise HTTPException(404, "Unknown calendar provider")
        if not settings.configured(provider):
            raise HTTPException(
                400, f"Add {provider.title()} OAuth credentials to the server configuration first."
            )
        verifier = secrets.token_urlsafe(64)
        state = store.oauth_start(session["id"], provider, verifier)
        extra = (
            {"access_type": "offline", "prompt": "consent select_account"}
            if provider == "google"
            else {"prompt": "select_account", "response_mode": "query"}
        )
        async with oauth_client(settings, provider) as client:
            url, _ = client.create_authorization_url(
                oauth_endpoints(settings, provider)[0], state=state, code_verifier=verifier, **extra
            )
        return {"url": url}

    @app.get("/oauth/{provider}/callback")
    async def callback(provider: str, request: Request, session=Depends(authenticated)):
        if provider not in {"google", "microsoft"}:
            raise HTTPException(404, "Unknown provider")
        verifier = store.oauth_consume(request.query_params.get("state", ""), session["id"], provider)
        if not verifier:
            raise HTTPException(400, "This connection link has expired or was already used. Start again.")
        if "error" in request.query_params:
            return RedirectResponse("/?connection=cancelled", status_code=303)
        code = request.query_params.get("code")
        if not code:
            raise HTTPException(400, "The provider did not return an authorization code.")
        try:
            subject, email, token = await exchange(settings, provider, code, verifier)
            async with engine.lock:
                store.add_account(provider, subject, email, token)
                store.set("paused", "true")
        except ProviderError:
            raise
        except Exception:
            raise HTTPException(
                400, "Connection failed. Check app registration and calendar permissions."
            ) from None
        return RedirectResponse("/?connection=success", status_code=303)

    @app.post("/api/accounts/{account_id}/enabled")
    async def enable(account_id: str, body: Enabled, session=Depends(authenticated)):
        async with engine.lock:
            if not store.enable(account_id, body.enabled):
                raise HTTPException(404, "Account not found")
            # Changing the set requires a fresh preview and an explicit resume.
            store.set("paused", "true")
        return {"ok": True}

    @app.post("/api/pause")
    async def pause(body: Paused, session=Depends(authenticated)):
        async with engine.lock:
            if not body.paused and sum(a["enabled"] for a in store.accounts()) < 2:
                raise HTTPException(400, "Enable at least two calendars first.")
            store.set("paused", "true" if body.paused else "false")
        return {"paused": body.paused}

    @app.post("/api/preview")
    async def preview(session=Depends(authenticated)):
        return await engine.preview()

    @app.post("/api/sync")
    async def sync(session=Depends(authenticated)):
        return await engine.run()

    @app.post("/api/accounts/{account_id}/disconnect")
    async def disconnect(account_id: str, session=Depends(authenticated)):
        await engine.disconnect(account_id)
        return {"ok": True}

    return app
