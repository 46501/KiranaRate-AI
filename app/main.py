"""
app/main.py
===========
FastAPI application entry point for AI Bill Scanner Automation.
Initializes configuration, storage, service adapters, background workers,
and registers both API and reviewer UI routes.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import router as api_router
from app.config import Settings, is_placeholder, settings
from app.models import (
    BillStatus,
    CostBasis,
    HeaderVerified,
    Reviewed,
    User,
)
from app.services.auth import (
    generate_csrf_token,
    hash_password,
    validate_email_format,
    validate_password_strength,
    validate_username,
    verify_csrf_token,
    verify_password,
)
from app.storage.base import BaseStore
from app.services.security import (
    check_rate_limit,
    get_client_ip,
    login_tracker,
    read_upload_file_safely,
    sanitize_user_facing_error,
    validate_image_magic_bytes,
)
from app.services.gemini_scanner import GeminiScanner, MockGeminiScanner
from app.services.sku_matcher import SKUMatcher
from app.services.tax_engine import TaxEngine
from app.services.whatsapp import MockWhatsAppClient, WhatsAppClient
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app.main")

# Project directories
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "ui" / "templates"
MEDIA_DIR = BASE_DIR / "media"

TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    Initializes storage, services, and registers them on app.state.
    """
    logger.info("Starting AI Bill Scanner Automation system...")
    cfg: Settings = settings

    # Storage initialization
    store = SQLiteStore(db_path=cfg.sqlite_db_path)
    store.create_tables()
    store.seed_demo_data()

    # WhatsApp adapter initialization
    if cfg.mock_whatsapp or is_placeholder(cfg.whatsapp_access_token):
        logger.info("Using MockWhatsAppClient (MOCK_WHATSAPP=true or token unset/placeholder)")
        whatsapp = MockWhatsAppClient(
            template_name=cfg.whatsapp_template_name,
            template_lang=cfg.whatsapp_template_lang,
        )
    else:
        whatsapp = WhatsAppClient(
            access_token=cfg.whatsapp_access_token,
            phone_number_id=cfg.whatsapp_phone_number_id,
            api_version=cfg.whatsapp_api_version,
            owner_number=cfg.whatsapp_owner_number,
            template_name=cfg.whatsapp_template_name,
            template_lang=cfg.whatsapp_template_lang,
            app_secret=cfg.whatsapp_app_secret,
        )

    # Gemini scanner initialization
    if cfg.mock_gemini or is_placeholder(cfg.gemini_api_key):
        logger.info("Using MockGeminiScanner (MOCK_GEMINI=true or key unset/placeholder)")
        scanner = MockGeminiScanner()
    else:
        scanner = GeminiScanner(
            api_key=cfg.gemini_api_key,
            model_name=cfg.gemini_model,
            request_timeout=cfg.gemini_request_timeout,
            max_image_bytes=cfg.bill_size_limit_bytes,
        )

    # Workflow Engine initialization
    engine = WorkflowEngine(
        store=store,
        scanner=scanner,
        whatsapp=whatsapp,
        settings=cfg,
    )

    # Attach to app.state for dependency injection
    app.state.settings = cfg
    app.state.store = store
    app.state.scanner = scanner
    app.state.whatsapp = whatsapp
    app.state.engine = engine
    app.state.scheduler = None

    # Background scheduler initialization (if enabled)
    if cfg.enable_background_workers:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            scheduler = AsyncIOScheduler()

            async def _scheduled_scan():
                try:
                    while await engine.run_scan_worker():
                        pass
                except Exception as exc:
                    logger.error("Error in scheduled scan worker: %s", exc)

            async def _scheduled_post():
                try:
                    while await engine.run_post_worker():
                        pass
                except Exception as exc:
                    logger.error("Error in scheduled post worker: %s", exc)

            async def _scheduled_alert():
                try:
                    while await engine.run_alert_worker():
                        pass
                except Exception as exc:
                    logger.error("Error in scheduled alert worker: %s", exc)

            scheduler.add_job(
                _scheduled_scan,
                "interval",
                seconds=cfg.worker_scan_interval_seconds,
                id="scan_worker",
                max_instances=1,
            )
            scheduler.add_job(
                _scheduled_post,
                "interval",
                seconds=cfg.worker_post_interval_seconds,
                id="post_worker",
                max_instances=1,
            )
            scheduler.add_job(
                _scheduled_alert,
                "interval",
                seconds=cfg.worker_alert_interval_seconds,
                id="alert_worker",
                max_instances=1,
            )
            scheduler.start()
            app.state.scheduler = scheduler
            logger.info("APScheduler background workers started successfully.")
        except Exception as exc:
            logger.error("Failed to start background scheduler: %s", exc)

    yield

    logger.info("Shutting down AI Bill Scanner Automation system...")
    if getattr(app.state, "scheduler", None) is not None:
        try:
            app.state.scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("Error shutting down scheduler: %s", exc)

    if getattr(app.state, "store", None) is not None and hasattr(app.state.store, "close"):
        try:
            app.state.store.close()
        except Exception as exc:
            logger.warning("Error closing store: %s", exc)


app = FastAPI(
    title="AI Bill Scanner Automation",
    description="Automated kirana/retail purchase bill intake, extraction, verification & price alerts.",
    version="1.0.0",
    lifespan=lifespan,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Attach standard HTTP security headers to all HTTP responses:
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: DENY
    - Referrer-Policy: strict-origin-when-cross-origin
    - Content-Security-Policy (allows safe inline and CDN assets used by dashboard)
    - X-XSS-Protection: 1; mode=block
    - Permissions-Policy
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        return response


# Security headers middleware
app.add_middleware(SecurityHeadersMiddleware)

# Secure session middleware for dashboard authentication & anti-fixation protection
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    session_cookie="kirana_session",
    max_age=14 * 24 * 3600,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch unhandled internal exceptions to sanitize error responses.
    Prevents file paths, database identifiers, and stack traces from reaching clients.
    """
    logger.exception("Unhandled server exception on %s %s: %s", request.method, request.url.path, exc)
    if request.url.path.startswith("/ui/"):
        return RedirectResponse(
            url="/ui/bills?msg=An+unexpected+internal+error+occurred",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return Response(
        content=json.dumps({"detail": "An internal server error occurred"}),
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        media_type="application/json",
    )

# Include JSON API routes
app.include_router(api_router, tags=["API"])


# ---------------------------------------------------------------------------
# Session & CSRF Security Helpers
# ---------------------------------------------------------------------------

def _get_session_user(request: Request) -> Optional[str]:
    """Retrieve logged in username from session, or None."""
    if hasattr(request, "session"):
        return request.session.get("authenticated_user")
    return None


def _require_ui_session(request: Request) -> Optional[str]:
    """
    Validate that user has an active session.
    Returns username if authorized, or None if authentication is required.
    """
    cfg: Settings = getattr(request.app.state, "settings", settings)
    user = _get_session_user(request)
    if user:
        return user
    if cfg.is_auth_enabled:
        return None
    # In open local dev mode with no credentials configured:
    return "admin"


def _get_or_create_csrf_token(request: Request) -> str:
    """Ensure a CSRF token exists on the session and return it."""
    if not hasattr(request, "session"):
        return ""
    token = request.session.get("csrf_token")
    if not token:
        token = generate_csrf_token()
        request.session["csrf_token"] = token
    return str(token)


# ---------------------------------------------------------------------------
# Authentication UI Routes (Login / Logout) & Protected Media
# ---------------------------------------------------------------------------

@app.get("/ui/login", response_class=HTMLResponse)
async def ui_login_page(request: Request, next: Optional[str] = None):
    """Render dashboard login form with rate limiting and first-time setup guidance."""
    cfg: Settings = getattr(request.app.state, "settings", settings)
    store: Optional[BaseStore] = getattr(request.app.state, "store", None)
    is_first_time_setup = (store.count_users() == 0) if store else False
    msg = request.query_params.get("msg")

    try:
        check_rate_limit(
            request,
            scope="login",
            max_requests=cfg.rate_limit_login_per_minute * 2,
            enabled=cfg.rate_limit_enabled,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": "",
                "next_url": next,
                "error": exc.detail,
                "is_first_time_setup": is_first_time_setup,
                "allow_registration": is_first_time_setup,
                "message": msg,
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": exc.headers.get("Retry-After", "60") if exc.headers else "60"},
        )

    if _get_session_user(request):
        return RedirectResponse(url=next or "/ui/bills", status_code=status.HTTP_303_SEE_OTHER)
    csrf_token = _get_or_create_csrf_token(request)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "csrf_token": csrf_token,
            "next_url": next,
            "error": None,
            "is_first_time_setup": is_first_time_setup,
            "allow_registration": is_first_time_setup,
            "message": msg,
        },
    )


@app.post("/ui/login", response_class=HTMLResponse)
async def ui_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    next: Optional[str] = Form(None),
):
    """Process dashboard login credentials, enforce brute-force protection, and set cookie."""
    cfg: Settings = getattr(request.app.state, "settings", settings)
    store: Optional[BaseStore] = getattr(request.app.state, "store", None)
    is_first_time_setup = (store.count_users() == 0) if store else False
    client_ip = get_client_ip(request)
    lockout_key = f"{username.strip().lower()}:{client_ip}"

    # 1. Enforce rate limiting on login endpoint
    try:
        check_rate_limit(
            request,
            scope="login",
            max_requests=cfg.rate_limit_login_per_minute,
            enabled=cfg.rate_limit_enabled,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": _get_or_create_csrf_token(request),
                "next_url": next,
                "error": exc.detail,
                "username": username,
                "is_first_time_setup": is_first_time_setup,
                "allow_registration": is_first_time_setup,
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": exc.headers.get("Retry-After", "60") if exc.headers else "60"},
        )

    # 2. Check account lockout from repeated failed attempts
    is_locked, retry_after = login_tracker.is_locked(lockout_key)
    if is_locked:
        logger.warning("Rejected login attempt for locked account username=%r from %s", username, client_ip)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": _get_or_create_csrf_token(request),
                "next_url": next,
                "error": f"Account temporarily locked due to repeated failed login attempts. Please retry in {retry_after} seconds.",
                "username": username,
                "is_first_time_setup": is_first_time_setup,
                "allow_registration": is_first_time_setup,
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        )

    # 3. Anti-CSRF verification
    session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
    if not verify_csrf_token(session_csrf, csrf_token):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": _get_or_create_csrf_token(request),
                "next_url": next,
                "error": "Invalid or expired CSRF token",
                "username": username,
                "is_first_time_setup": is_first_time_setup,
                "allow_registration": is_first_time_setup,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    # 4. Credentials validation
    db_user = store.get_user_by_identifier(username.strip()) if store else None

    valid_user = False
    valid_pass = False
    authenticated_username = username.strip()
    user_id = ""
    user_role = "admin"

    if db_user:
        valid_user = True
        valid_pass = verify_password(password, db_user.password_hash)
        authenticated_username = db_user.username
        user_id = db_user.user_id
        user_role = db_user.role
    elif store and store.count_users() == 0:
        # Fallback to config settings strictly when NO database users exist (backward compatibility for test suites)
        expected_username = cfg.dashboard_username or "admin"
        expected_cred = cfg.dashboard_password_hash or cfg.dashboard_password
        valid_user = hmac.compare_digest(username.strip(), expected_username.strip())
        valid_pass = verify_password(password, expected_cred) if expected_cred else False
        authenticated_username = expected_username.strip()
        user_id = "usr_legacy_admin"

    if not (valid_user and valid_pass):
        is_now_locked, retry_secs = login_tracker.record_failure(
            lockout_key,
            max_failed=cfg.login_max_failed_attempts,
            lockout_secs=cfg.login_lockout_seconds,
        )
        logger.warning("Failed dashboard login attempt for username=%r from %s", username, client_ip)
        if is_now_locked:
            err_msg = f"Account temporarily locked due to repeated failed login attempts. Please retry in {retry_secs} seconds."
            status_code = status.HTTP_429_TOO_MANY_REQUESTS
            headers = {"Retry-After": str(retry_secs)}
        else:
            err_msg = "Invalid username or password"
            status_code = status.HTTP_401_UNAUTHORIZED
            headers = None

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": _get_or_create_csrf_token(request),
                "next_url": next,
                "error": err_msg,
                "username": username,
                "is_first_time_setup": is_first_time_setup,
                "allow_registration": is_first_time_setup,
            },
            status_code=status_code,
            headers=headers,
        )

    # 5. Clear failed attempt tracker on success
    login_tracker.record_success(lockout_key)

    # Prevent session fixation: regenerate session completely before storing user state
    request.session.clear()
    request.session["authenticated_user"] = authenticated_username
    request.session["user_id"] = user_id
    request.session["user_role"] = user_role
    request.session["csrf_token"] = generate_csrf_token()
    request.session["login_time"] = datetime.now(timezone.utc).isoformat()
    logger.info("Successful dashboard login for username=%r (role=%s)", authenticated_username, user_role)

    target = next if (next and next.startswith("/") and not next.startswith("//")) else "/ui/bills"
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/ui/register", response_class=HTMLResponse)
async def ui_register_page(request: Request):
    """
    Public web registration is permanently disabled.
    Administrators must be provisioned via the secure local CLI: python -m app.cli create-admin.
    """
    return RedirectResponse(
        url="/ui/login?msg=Public+web+registration+is+disabled.+Run+'python+-m+app.cli+create-admin'+in+your+server+terminal.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/register", response_class=HTMLResponse)
async def ui_register_submit(request: Request):
    """Reject public web registration attempts."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Public web registration is disabled. Administrator provisioning is restricted to the local CLI ('python -m app.cli create-admin').",
    )


@app.get("/ui/account", response_class=HTMLResponse)
async def ui_account_page(request: Request):
    """
    Render secure administrator account profile and settings page.
    Requires active authenticated session with admin role.
    """
    cfg: Settings = getattr(request.app.state, "settings", settings)
    user_name = _get_session_user(request)
    if not user_name and cfg.is_auth_enabled:
        return RedirectResponse(url="/ui/login?next=/ui/account", status_code=status.HTTP_303_SEE_OTHER)

    user_role = request.session.get("user_role", "admin")
    if user_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Administrator privileges required.")

    store: Optional[BaseStore] = getattr(request.app.state, "store", None)
    user = None
    if store:
        user_id = request.session.get("user_id")
        if user_id:
            user = store.get_user_by_id(user_id)
        if not user:
            user = store.get_user_by_identifier(user_name)

    # Fallback user representation for headless test sessions
    if not user:
        user = User(
            user_id="usr_admin",
            username=user_name or "admin",
            email="admin@store.local",
            password_hash="***",
            role="admin",
            created_at=datetime.now(timezone.utc),
        )

    csrf_token = _get_or_create_csrf_token(request)
    msg = request.query_params.get("msg")
    err = request.query_params.get("err")

    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "user": user,
            "csrf_token": csrf_token,
            "current_user": user_name,
            "current_user_role": user_role,
            "message": msg,
            "error": err,
        },
    )


@app.post("/ui/account/change-password")
async def ui_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_new_password: str = Form(...),
    csrf_token: str = Form(""),
):
    """
    Change administrator password after verifying the current password.
    Enforces password strength rules and invalidates old credentials.
    """
    cfg: Settings = getattr(request.app.state, "settings", settings)
    user_name = _get_session_user(request)
    if not user_name and cfg.is_auth_enabled:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        check_rate_limit(request, scope="account", max_requests=10, enabled=cfg.rate_limit_enabled)
    except HTTPException as exc:
        return RedirectResponse(url=f"/ui/account?err={quote(exc.detail)}", status_code=status.HTTP_303_SEE_OTHER)

    session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
    if not verify_csrf_token(session_csrf, csrf_token):
        return RedirectResponse(url="/ui/account?err=Invalid+or+expired+CSRF+token", status_code=status.HTTP_303_SEE_OTHER)

    store: Optional[BaseStore] = getattr(request.app.state, "store", None)
    user = None
    if store:
        user_id = request.session.get("user_id")
        if user_id:
            user = store.get_user_by_id(user_id)
        if not user:
            user = store.get_user_by_identifier(user_name)

    if not user:
        return RedirectResponse(url="/ui/account?err=User+account+not+found+in+database", status_code=status.HTTP_303_SEE_OTHER)

    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/ui/account?err=Current+password+is+incorrect", status_code=status.HTTP_303_SEE_OTHER)

    if new_password != confirm_new_password:
        return RedirectResponse(url="/ui/account?err=New+passwords+do+not+match", status_code=status.HTTP_303_SEE_OTHER)

    valid_p, p_err = validate_password_strength(new_password)
    if not valid_p:
        return RedirectResponse(url=f"/ui/account?err={quote(p_err)}", status_code=status.HTTP_303_SEE_OTHER)

    new_hash = hash_password(new_password)
    store.update_user_password(user.user_id, new_hash)
    logger.info("Administrator password successfully updated for username=%r", user.username)

    # Refresh session login time
    request.session["login_time"] = datetime.now(timezone.utc).isoformat()
    return RedirectResponse(url="/ui/account?msg=Password+successfully+updated.", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/ui/account/update-email")
async def ui_update_email(
    request: Request,
    new_email: str = Form(...),
    current_password: str = Form(...),
    csrf_token: str = Form(""),
):
    """
    Update administrator email address with password confirmation.
    """
    cfg: Settings = getattr(request.app.state, "settings", settings)
    user_name = _get_session_user(request)
    if not user_name and cfg.is_auth_enabled:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        check_rate_limit(request, scope="account", max_requests=10, enabled=cfg.rate_limit_enabled)
    except HTTPException as exc:
        return RedirectResponse(url=f"/ui/account?err={quote(exc.detail)}", status_code=status.HTTP_303_SEE_OTHER)

    session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
    if not verify_csrf_token(session_csrf, csrf_token):
        return RedirectResponse(url="/ui/account?err=Invalid+or+expired+CSRF+token", status_code=status.HTTP_303_SEE_OTHER)

    store: Optional[BaseStore] = getattr(request.app.state, "store", None)
    user = None
    if store:
        user_id = request.session.get("user_id")
        if user_id:
            user = store.get_user_by_id(user_id)
        if not user:
            user = store.get_user_by_identifier(user_name)

    if not user:
        return RedirectResponse(url="/ui/account?err=User+account+not+found+in+database", status_code=status.HTTP_303_SEE_OTHER)

    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/ui/account?err=Current+password+is+incorrect", status_code=status.HTTP_303_SEE_OTHER)

    clean_email = new_email.strip().lower()
    valid_e, e_err = validate_email_format(clean_email)
    if not valid_e:
        return RedirectResponse(url=f"/ui/account?err={quote(e_err)}", status_code=status.HTTP_303_SEE_OTHER)

    existing = store.get_user_by_email(clean_email)
    if existing and existing.user_id != user.user_id:
        return RedirectResponse(url="/ui/account?err=Email+address+is+already+in+use+by+another+account", status_code=status.HTTP_303_SEE_OTHER)

    store.update_user_email(user.user_id, clean_email)
    logger.info("Administrator email successfully updated to %r for user_id=%r", clean_email, user.user_id)
    return RedirectResponse(url="/ui/account?msg=Email+address+successfully+updated.", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/ui/logout")
async def ui_logout(request: Request):
    """Log out of dashboard and clear session cookie."""
    if hasattr(request, "session"):
        request.session.clear()
    return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/ui/media/{bill_id}")
async def ui_get_bill_media(
    bill_id: str,
    request: Request,
):
    """
    Serve invoice images strictly to authenticated sessions or authorized API callers.
    Guarantees strict path traversal containment within MEDIA_DIR.
    """
    cfg: Settings = getattr(request.app.state, "settings", settings)
    user = _get_session_user(request)
    if not user and cfg.is_auth_enabled:
        auth_h = request.headers.get("Authorization", "").strip()
        key_h = request.headers.get("X-API-Key", "").strip()
        token = auth_h[7:].strip() if auth_h.startswith("Bearer ") else key_h
        if not (token and cfg.api_key and hmac.compare_digest(token, cfg.api_key)):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required to view invoice image",
                headers={"WWW-Authenticate": "Bearer"},
            )

    media_jpg = (MEDIA_DIR / f"{bill_id}.jpg").resolve()
    media_png = (MEDIA_DIR / f"{bill_id}.png").resolve()
    media_dir_resolved = MEDIA_DIR.resolve()

    if not (media_jpg.is_relative_to(media_dir_resolved) and media_png.is_relative_to(media_dir_resolved)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bill ID format")

    if media_jpg.exists():
        return FileResponse(media_jpg, media_type="image/jpeg")
    elif media_png.exists():
        return FileResponse(media_png, media_type="image/png")
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Invoice image for bill '{bill_id}' not found on disk")


# ---------------------------------------------------------------------------
# Reviewer Web UI Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
@app.get("/ui/bills", response_class=HTMLResponse)
async def ui_bills_dashboard(
    request: Request,
    status_filter: Optional[str] = None,
    view: Optional[str] = None,
):
    """Render KiranaIQ purchase intelligence dashboard with multi-view navigation."""
    user = _require_ui_session(request)
    if not user:
        next_path = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(url=f"/ui/login?next={next_path}", status_code=status.HTTP_303_SEE_OTHER)

    store: SQLiteStore = request.app.state.store
    filter_enum = None
    if status_filter and status_filter.upper() != "ALL":
        try:
            filter_enum = BillStatus(status_filter.upper())
        except ValueError:
            pass

    all_bills = store.list_bills()
    if filter_enum:
        bills = [b for b in all_bills if b.status == filter_enum]
    else:
        bills = all_bills

    suppliers = store.list_suppliers()
    products = store.list_products()
    purchases = store.list_purchases(limit=50)
    alerts = store.list_alerts(limit=50)
    pending_alert = store.get_oldest_pending_alert()

    active_suppliers_count = sum(1 for s in suppliers if getattr(s.active, "value", s.active) == "YES")

    metrics = {
        "total": len(all_bills),
        "queued": sum(1 for b in all_bills if b.status == BillStatus.QUEUED),
        "scanning": sum(1 for b in all_bills if b.status == BillStatus.SCANNING),
        "review": sum(1 for b in all_bills if b.status == BillStatus.REVIEW),
        "approved": sum(1 for b in all_bills if b.status == BillStatus.APPROVED),
        "posted": sum(1 for b in all_bills if b.status == BillStatus.POSTED),
        "rejected": sum(1 for b in all_bills if b.status == BillStatus.REJECTED),
        "active_suppliers": active_suppliers_count,
        "alerts_count": len(alerts),
    }

    status_counts = {
        "ALL": len(all_bills),
        **{s.value: sum(1 for b in all_bills if b.status == s) for s in BillStatus},
    }

    # Format current date in IST
    from datetime import datetime
    import pytz
    ist_tz = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist_tz).strftime("%A, %d %B %Y")

    # Scheduler info
    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_running = scheduler.running if scheduler else False

    # SKU Price Intelligence Summary
    sku_analytics = []
    for prod in products:
        p_list = store.get_purchases_for_sku(prod.sku)
        if p_list:
            rates_inr = [round(p.net_paise / (p.units * 100), 2) for p in p_list if p.units > 0]
            min_r = min(rates_inr)
            max_r = max(rates_inr)
            latest_r = rates_inr[-1]
            volatility = round(((max_r - min_r) / min_r) * 100, 2) if min_r > 0 else 0.0
            sku_analytics.append({
                "sku": prod.sku,
                "product_name": prod.product_name,
                "count": len(p_list),
                "latest_rate": latest_r,
                "min_rate": min_r,
                "max_rate": max_r,
                "volatility": volatility,
                "purchases": p_list,
            })

    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "bills": bills,
            "all_bills": all_bills,
            "status_filter": status_filter or "ALL",
            "statuses": [s.value for s in BillStatus],
            "metrics": metrics,
            "status_counts": status_counts,
            "has_pending_alert": pending_alert is not None,
            "pending_alert": pending_alert,
            "suppliers": suppliers,
            "products": products,
            "purchases": purchases,
            "sku_analytics": sku_analytics,
            "gst_report": TaxEngine.compute_gst_report(store),
            "alerts": alerts,
            "current_view": view or "overview",
            "now_ist": now_ist,
            "scheduler_running": scheduler_running,
            "settings": request.app.state.settings,
            "csrf_token": _get_or_create_csrf_token(request),
            "current_user": user,
        },
    )


@app.post("/ui/upload")
async def ui_upload_bill(
    request: Request,
    file: UploadFile = File(...),
    scan_now: bool = Form(False),
    csrf_token: Optional[str] = Form(None),
):
    """Handle direct file upload from web UI and optionally trigger scan immediately."""
    user = _require_ui_session(request)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    cfg: Settings = getattr(request.app.state, "settings", settings)
    if cfg.is_auth_enabled:
        session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
        token = csrf_token or request.headers.get("X-CSRF-Token")
        if not verify_csrf_token(session_csrf, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")

    # Rate limiting check on UI uploads
    try:
        check_rate_limit(
            request,
            scope="upload",
            max_requests=cfg.rate_limit_upload_per_minute,
            enabled=cfg.rate_limit_enabled,
        )
    except HTTPException as exc:
        return RedirectResponse(
            url=f"/ui/bills?msg=Upload+failed:+{quote(exc.detail)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Memory-safe upload reading with Content-Length check and 5 MB limit
    try:
        content = await read_upload_file_safely(
            file=file,
            max_bytes=cfg.bill_size_limit_bytes,
            request=request,
        )
    except HTTPException as exc:
        return RedirectResponse(
            url=f"/ui/bills?msg=Upload+failed:+{quote(exc.detail)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if not content:
        return RedirectResponse(
            url="/ui/bills?msg=Upload+failed:+Uploaded+file+was+empty",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Validate binary magic bytes (JPEG or PNG)
    try:
        detected_mime = validate_image_magic_bytes(content)
    except ValueError as exc:
        safe_msg = sanitize_user_facing_error(str(exc))
        return RedirectResponse(
            url=f"/ui/bills?msg=Upload+failed:+{quote(safe_msg)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    engine: WorkflowEngine = request.app.state.engine
    try:
        bill_id = await engine.intake_uploaded_bill(
            image_bytes=content,
            filename=file.filename or "uploaded_bill.jpg",
            mime_type=detected_mime,
            sender="web-dashboard",
        )
        if scan_now:
            await engine.run_scan_worker()
            return RedirectResponse(
                url=f"/ui/bills/{bill_id}?msg=Bill+uploaded+and+scanned+successfully",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        return RedirectResponse(
            url=f"/ui/bills?status_filter=QUEUED&msg=Bill+{bill_id}+uploaded+successfully+and+queued+for+scan",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except ValueError as exc:
        safe_msg = sanitize_user_facing_error(str(exc))
        return RedirectResponse(
            url=f"/ui/bills?msg=Upload+failed:+{quote(safe_msg)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except Exception as exc:
        logger.exception("Web UI upload failed: %s", exc)
        return RedirectResponse(
            url="/ui/bills?msg=Upload+failed:+An+internal+server+error+occurred",
            status_code=status.HTTP_303_SEE_OTHER,
        )


@app.get("/ui/bills/{bill_id}", response_class=HTMLResponse)
async def ui_bill_detail(request: Request, bill_id: str, msg: Optional[str] = None):
    """Render detailed bill inspection and review form."""
    user = _require_ui_session(request)
    if not user:
        next_path = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(url=f"/ui/login?next={next_path}", status_code=status.HTTP_303_SEE_OTHER)

    store: SQLiteStore = request.app.state.store
    bill = store.get_bill(bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")

    lines = store.get_review_lines(bill_id)
    suppliers = store.list_suppliers(active_only=True)
    products = store.list_products(active_only=True)

    # Compute intelligent fuzzy SKU suggestions for unmapped lines
    suggestions = {}
    for ln in lines:
        best = SKUMatcher.get_best_match(ln.product_raw, ln.pack_raw, products, auto_threshold=0.45)
        if best:
            suggestions[ln.review_id] = {
                "sku": best.sku,
                "product_name": best.product_name,
                "confidence": best.confidence_percent,
                "matched_pack": best.matched_pack,
            }

    # Check for image file on disk
    image_url = None
    media_jpg = MEDIA_DIR / f"{bill.bill_id}.jpg"
    media_png = MEDIA_DIR / f"{bill.bill_id}.png"
    if media_jpg.exists() or media_png.exists():
        image_url = f"/ui/media/{bill.bill_id}"

    return templates.TemplateResponse(
        request=request,
        name="bill_detail.html",
        context={
            "bill": bill,
            "lines": lines,
            "suppliers": suppliers,
            "products": products,
            "suggestions": suggestions,
            "msg": msg,
            "image_url": image_url,
            "csrf_token": _get_or_create_csrf_token(request),
            "current_user": user,
        },
    )


@app.post("/ui/bills/{bill_id}/approve")
async def ui_approve_bill(
    request: Request,
    bill_id: str,
):
    """Handle HTML form submission for bill approval."""
    user = _require_ui_session(request)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    form_data = await request.form()
    cfg: Settings = getattr(request.app.state, "settings", settings)
    if cfg.is_auth_enabled:
        token = form_data.get("csrf_token") or request.headers.get("X-CSRF-Token")
        session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
        if not verify_csrf_token(session_csrf, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")

    store: SQLiteStore = request.app.state.store

    reviewer = str(form_data.get("reviewer", "")).strip()
    supplier_id = str(form_data.get("supplier_id", "")).strip()
    invoice_no = str(form_data.get("invoice_no", "")).strip()
    invoice_date_raw = str(form_data.get("invoice_date", "")).strip()
    verified_net_total_raw = str(form_data.get("verified_net_total", "")).strip()
    header_verified_raw = str(form_data.get("header_verified", "NO")).strip().upper()

    if not reviewer or not supplier_id or not invoice_no or not invoice_date_raw:
        return RedirectResponse(
            url=f"/ui/bills/{bill_id}?msg=Error:+All+header+fields+are+required",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        inv_date = date.fromisoformat(invoice_date_raw)
    except ValueError:
        return RedirectResponse(
            url=f"/ui/bills/{bill_id}?msg=Error:+Invalid+date+format",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        net_total = Decimal(verified_net_total_raw)
    except Exception:
        return RedirectResponse(
            url=f"/ui/bills/{bill_id}?msg=Error:+Invalid+verified+net+total",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Process line updates from form
    lines = store.get_review_lines(bill_id)
    for ln in lines:
        sku = str(form_data.get(f"sku_{ln.review_id}", "")).strip()
        units_str = str(form_data.get(f"units_{ln.review_id}", "")).strip()
        amt_str = str(form_data.get(f"amt_{ln.review_id}", "")).strip()
        rev_checked = form_data.get(f"reviewed_{ln.review_id}") is not None

        units = int(units_str) if units_str.isdigit() else 0
        amt = Decimal(amt_str) if amt_str else Decimal("0")

        store.update_review_line(
            ln.review_id,
            sku=sku,
            received_units=units,
            net_amount=amt,
            reviewed=Reviewed.YES if rev_checked else Reviewed.NO,
        )

    # Update bill and mark APPROVED
    store.update_bill(
        bill_id,
        reviewer=reviewer,
        supplier_id=supplier_id,
        cost_basis=CostBasis.EX_GST,
        verified_net_total=net_total,
        header_verified=HeaderVerified.YES if header_verified_raw == "YES" else HeaderVerified.NO,
        invoice_date=inv_date.isoformat(),
        invoice_no=invoice_no,
        currency="INR",
        status=BillStatus.APPROVED,
        reason=None,
    )

    try:
        engine: WorkflowEngine = request.app.state.engine
        await engine.run_post_worker()
        await engine.run_alert_worker()
        msg = f"Bill+{bill_id}+successfully+approved+and+posted+to+purchase+ledger!"
    except Exception as exc:
        logger.warning("Auto post worker failed after approval: %s", exc)
        msg = f"Bill+{bill_id}+successfully+approved!+Ready+for+posting."

    return RedirectResponse(
        url=f"/ui/bills/{bill_id}?msg={msg}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/bills/{bill_id}/reject")
async def ui_reject_bill(
    request: Request,
    bill_id: str,
    reviewer: str = Form(...),
    reason: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Handle HTML form submission for bill rejection."""
    user = _require_ui_session(request)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    cfg: Settings = getattr(request.app.state, "settings", settings)
    if cfg.is_auth_enabled:
        token = csrf_token or request.headers.get("X-CSRF-Token")
        session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
        if not verify_csrf_token(session_csrf, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")

    store: SQLiteStore = request.app.state.store
    store.update_bill(
        bill_id,
        reviewer=reviewer.strip(),
        reason=reason.strip(),
        status=BillStatus.REJECTED,
    )
    return RedirectResponse(
        url=f"/ui/bills/{bill_id}?msg=Bill+marked+as+REJECTED",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/bills/{bill_id}/quick-approve")
async def ui_quick_approve_bill(
    request: Request,
    bill_id: str,
    csrf_token: Optional[str] = Form(None),
):
    """
    1-Click Quick Approval from Dashboard for bills with high-confidence extractions.
    Automatically assigns suggested SKUs, marks header verified, balances paise,
    and transitions status to APPROVED (and triggers Post Worker).
    """
    user = _require_ui_session(request)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    cfg: Settings = getattr(request.app.state, "settings", settings)
    if cfg.is_auth_enabled:
        token = csrf_token or request.headers.get("X-CSRF-Token")
        session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
        if not verify_csrf_token(session_csrf, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")

    store: SQLiteStore = request.app.state.store
    engine: WorkflowEngine = request.app.state.engine

    bill = store.get_bill(bill_id)
    if not bill:
        return RedirectResponse(url="/ui/bills?msg=Error:+Bill+not+found", status_code=status.HTTP_303_SEE_OTHER)

    if bill.status != BillStatus.REVIEW:
        return RedirectResponse(
            url=f"/ui/bills?msg=Bill+{bill_id}+is+in+{bill.status.value}+state+and+cannot+be+quick-approved",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    lines = store.get_review_lines(bill_id)
    if not lines:
        return RedirectResponse(
            url=f"/ui/bills/{bill_id}?msg=Error:+Cannot+quick-approve+bill+with+no+line+items",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Resolve supplier
    suppliers = store.list_suppliers(active_only=True)
    supplier_id = None
    if bill.supplier_raw:
        norm_raw = bill.supplier_raw.lower().strip()
        for s in suppliers:
            if norm_raw in s.supplier_name.lower() or s.supplier_name.lower() in norm_raw or s.supplier_id.lower() in norm_raw:
                supplier_id = s.supplier_id
                break
    if not supplier_id:
        supplier_id = bill.supplier_id
    if not supplier_id and suppliers:
        supplier_id = suppliers[0].supplier_id

    # Resolve products & lines
    products = store.list_products(active_only=True)
    running_total = Decimal("0.00")

    for ln in lines:
        sku = ln.sku
        if not sku:
            best = SKUMatcher.get_best_match(ln.product_raw, ln.pack_raw, products, auto_threshold=0.35)
            sku = best.sku if best else (products[0].sku if products else "UNKNOWN")

        units = ln.received_units or int(Decimal(str(ln.quantity_raw or "1")).to_integral_value())
        if units < 1:
            units = 1

        amt = ln.net_amount
        if not amt:
            try:
                amt = Decimal(str(ln.line_total_raw or "0.00").strip())
            except Exception:
                amt = Decimal("0.00")
            if amt <= 0 and ln.rate_raw:
                try:
                    amt = Decimal(str(ln.rate_raw).strip()) * units
                except Exception:
                    amt = Decimal("0.00")

        running_total += amt
        store.update_review_line(
            ln.review_id,
            sku=sku,
            received_units=units,
            net_amount=amt,
            reviewed=Reviewed.YES,
        )

    inv_date = bill.invoice_date or date.today().isoformat()
    verified_total = bill.verified_net_total or (Decimal(str(bill.invoice_total)) if bill.invoice_total else running_total)
    if verified_total <= 0:
        verified_total = running_total

    store.update_bill(
        bill_id,
        supplier_id=supplier_id,
        invoice_no=bill.invoice_no or f"INV-{bill_id[-6:]}",
        invoice_date=inv_date,
        cost_basis=CostBasis.EX_GST,
        verified_net_total=verified_total,
        header_verified=HeaderVerified.YES,
        reviewer="quick_approver",
        status=BillStatus.APPROVED,
    )

    try:
        await engine.run_post_worker()
        msg = f"Bill+{bill_id}+Quick-Approved+&+Posted+Successfully!"
    except Exception as exc:
        logger.warning("Auto post worker failed after quick approval: %s", exc)
        msg = f"Bill+{bill_id}+Quick-Approved+Successfully!"

    return RedirectResponse(
        url=f"/ui/bills?status_filter=ALL&msg={msg}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/bills/{bill_id}/retry")
async def ui_retry_bill(
    request: Request,
    bill_id: str,
    csrf_token: Optional[str] = Form(None),
):
    """Handle HTML form submission to retry a failed or stuck bill scan."""
    user = _require_ui_session(request)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    cfg: Settings = getattr(request.app.state, "settings", settings)
    if cfg.is_auth_enabled:
        token = csrf_token or request.headers.get("X-CSRF-Token")
        session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
        if not verify_csrf_token(session_csrf, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")

    store: SQLiteStore = request.app.state.store
    bill = store.get_bill(bill_id)
    if not bill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found")

    # Purge any partial review lines and reset to QUEUED
    store.delete_review_lines(bill_id)
    store.update_bill(bill_id, status=BillStatus.QUEUED, notes=None)

    # Trigger scan worker immediately
    engine: WorkflowEngine = request.app.state.engine
    await engine.run_scan_worker()

    return RedirectResponse(
        url=f"/ui/bills/{bill_id}?msg=scan_retried",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/workers/run/{stage}")
async def ui_trigger_worker(
    request: Request,
    stage: str,
    csrf_token: Optional[str] = Form(None),
):
    """Trigger worker and redirect back to dashboard."""
    user = _require_ui_session(request)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    cfg: Settings = getattr(request.app.state, "settings", settings)
    if cfg.is_auth_enabled:
        token = csrf_token or request.headers.get("X-CSRF-Token")
        session_csrf = request.session.get("csrf_token") if hasattr(request, "session") else None
        if not verify_csrf_token(session_csrf, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")

    # Rate limiting on worker triggers
    check_rate_limit(
        request,
        scope="worker",
        max_requests=cfg.rate_limit_worker_per_minute,
        enabled=cfg.rate_limit_enabled,
    )

    engine: WorkflowEngine = request.app.state.engine
    stage_norm = stage.lower().strip()
    msg = f"Ran {stage_norm} worker"
    if stage_norm == "scan":
        res = await engine.run_scan_worker()
        msg += f": processed bill {res}" if res else ": no bills queued"
    elif stage_norm == "post":
        res = await engine.run_post_worker()
        msg += f": processed bill {res}" if res else ": no bills approved"
    elif stage_norm == "alert":
        res = await engine.run_alert_worker()
        msg += f": processed alert {res}" if res else ": no alerts pending"

    return RedirectResponse(
        url=f"/ui/bills?status_filter=ALL&msg={msg}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
