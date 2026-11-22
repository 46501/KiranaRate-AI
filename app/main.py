"""
app/main.py
===========
FastAPI application entry point for AI Bill Scanner Automation.
Initializes configuration, storage, service adapters, background workers,
and registers both API and reviewer UI routes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.routes import router as api_router
from app.config import Settings, settings
from app.models import (
    BillStatus,
    CostBasis,
    HeaderVerified,
    Reviewed,
)
from app.services.gemini_scanner import GeminiScanner, MockGeminiScanner
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
    if cfg.mock_whatsapp or not cfg.whatsapp_access_token:
        logger.info("Using MockWhatsAppClient (MOCK_WHATSAPP=true or token unset)")
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
        )

    # Gemini scanner initialization
    if cfg.mock_gemini or not cfg.gemini_api_key:
        logger.info("Using MockGeminiScanner (MOCK_GEMINI=true or key unset)")
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

# Include JSON API routes
app.include_router(api_router, tags=["API"])

# Mount media directory for invoice previews
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")


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
            "alerts": alerts,
            "current_view": view or "overview",
            "now_ist": now_ist,
            "scheduler_running": scheduler_running,
            "settings": request.app.state.settings,
        },
    )


@app.get("/ui/bills/{bill_id}", response_class=HTMLResponse)
async def ui_bill_detail(request: Request, bill_id: str, msg: Optional[str] = None):
    """Render detailed bill inspection and review form."""
    store: SQLiteStore = request.app.state.store
    bill = store.get_bill(bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")

    lines = store.get_review_lines(bill_id)
    suppliers = store.list_suppliers(active_only=True)
    products = store.list_products(active_only=True)

    # Check for image file on disk
    image_url = None
    media_jpg = MEDIA_DIR / f"{bill.bill_id}.jpg"
    media_png = MEDIA_DIR / f"{bill.bill_id}.png"
    if media_jpg.exists():
        image_url = f"/media/{bill.bill_id}.jpg"
    elif media_png.exists():
        image_url = f"/media/{bill.bill_id}.png"

    return templates.TemplateResponse(
        request=request,
        name="bill_detail.html",
        context={
            "bill": bill,
            "lines": lines,
            "suppliers": suppliers,
            "products": products,
            "msg": msg,
            "image_url": image_url,
        },
    )


@app.post("/ui/bills/{bill_id}/approve")
async def ui_approve_bill(
    request: Request,
    bill_id: str,
):
    """Handle HTML form submission for bill approval."""
    store: SQLiteStore = request.app.state.store
    form_data = await request.form()

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

    return RedirectResponse(
        url=f"/ui/bills/{bill_id}?msg=Bill+successfully+approved!+Ready+for+posting.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/bills/{bill_id}/reject")
async def ui_reject_bill(
    request: Request,
    bill_id: str,
    reviewer: str = Form(...),
    reason: str = Form(...),
):
    """Handle HTML form submission for bill rejection."""
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


@app.post("/ui/workers/run/{stage}")
async def ui_trigger_worker(request: Request, stage: str):
    """Trigger worker and redirect back to dashboard."""
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
