"""
app/api/routes.py
=================
FastAPI route definitions for the AI Bill Scanner Automation system:
- Health check
- WhatsApp webhook (GET verification & POST intake)
- Bills management (listing, single detail, approval, rejection)
- Manual worker triggers (scan, post, alert)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.api.dependencies import get_engine, get_settings, get_store
from app.config import Settings
from app.models import Bill, BillStatus, CostBasis, HeaderVerified, Reviewed, ReviewLine
from app.schemas import (
    ApproveRequest,
    BillResponse,
    RejectRequest,
    ReviewLineResponse,
)
from app.services.workflow_engine import WorkflowEngine
from app.storage.base import BaseStore

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------

def _bill_to_response(bill: Bill, lines: Optional[List[ReviewLine]] = None) -> BillResponse:
    lines_resp = None
    if lines is not None:
        lines_resp = [
            ReviewLineResponse(
                review_id=ln.review_id,
                bill_id=ln.bill_id,
                scan_id=ln.scan_id,
                line_no=ln.line_no,
                product_raw=ln.product_raw,
                pack_raw=ln.pack_raw,
                quantity_raw=ln.quantity_raw,
                unit_raw=ln.unit_raw,
                rate_raw=ln.rate_raw,
                line_total_raw=ln.line_total_raw,
                sku=ln.sku,
                received_units=ln.received_units,
                net_amount=ln.net_amount,
                reviewed=ln.reviewed,
            )
            for ln in lines
        ]

    return BillResponse(
        bill_id=bill.bill_id,
        sender=bill.sender,
        media_id=bill.media_id,
        mime_type=bill.mime_type,
        received_at=bill.received_at,
        status=bill.status,
        scan_id=bill.scan_id,
        scan_started_at=bill.scan_started_at,
        supplier_raw=bill.supplier_raw,
        invoice_no=bill.invoice_no,
        invoice_date=bill.invoice_date,
        currency=bill.currency,
        invoice_total=bill.invoice_total,
        notes=bill.notes,
        line_count=bill.line_count,
        supplier_id=bill.supplier_id,
        cost_basis=bill.cost_basis,
        verified_net_total=bill.verified_net_total,
        header_verified=bill.header_verified,
        reviewer=bill.reviewer,
        posted_at=bill.posted_at,
        reason=bill.reason,
        lines=lines_resp,
    )


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

@router.get("/health")
async def health_check() -> dict:
    """Service health endpoint."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# WhatsApp Webhook
# ---------------------------------------------------------------------------

@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
) -> Response:
    """
    Handle Meta WhatsApp webhook subscription verification challenge.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        if hub_challenge is not None:
            return PlainTextResponse(content=hub_challenge, status_code=status.HTTP_200_OK)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed")


@router.post("/webhook/whatsapp")
async def receive_whatsapp_webhook(
    payload: dict,
    engine: WorkflowEngine = Depends(get_engine),
) -> dict:
    """
    Inbound WhatsApp webhook endpoint.
    Processes image bills asynchronously and returns 200 OK.
    """
    try:
        inserted_bill_ids = await engine.run_intake(payload)
        return {"status": "ok", "inserted_count": len(inserted_bill_ids), "bill_ids": inserted_bill_ids}
    except Exception as exc:
        logger.exception("Error processing intake webhook: %s", exc)
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Bills Endpoints
# ---------------------------------------------------------------------------

@router.get("/bills", response_model=List[BillResponse])
async def list_bills(
    status: Optional[BillStatus] = None,
    store: BaseStore = Depends(get_store),
) -> List[BillResponse]:
    """List all bills, optionally filtered by status (oldest first)."""
    bills = store.list_bills(status=status)
    return [_bill_to_response(b) for b in bills]


@router.get("/bills/{bill_id}", response_model=BillResponse)
async def get_bill_detail(
    bill_id: str,
    store: BaseStore = Depends(get_store),
) -> BillResponse:
    """Fetch single bill detail including its review lines."""
    bill = store.get_bill(bill_id)
    if bill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Bill '{bill_id}' not found")
    lines = store.get_review_lines(bill_id)
    return _bill_to_response(bill, lines=lines)


@router.post("/bills/{bill_id}/approve", response_model=BillResponse)
async def approve_bill(
    bill_id: str,
    req: ApproveRequest,
    store: BaseStore = Depends(get_store),
) -> BillResponse:
    """
    Approve a bill from REVIEW to APPROVED status.
    Optionally updates line items with mapped SKUs, received units, and net amounts.
    """
    bill = store.get_bill(bill_id)
    if bill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Bill '{bill_id}' not found")

    # Update line items if provided in request
    if req.line_updates:
        for item in req.line_updates:
            store.update_review_line(
                review_id=item.review_id,
                sku=item.sku,
                received_units=item.received_units,
                net_amount=item.net_amount,
                reviewed=item.reviewed,
            )

    # Update bill fields and transition to APPROVED
    store.update_bill(
        bill_id,
        reviewer=req.reviewer,
        supplier_id=req.supplier_id,
        cost_basis=req.cost_basis,
        verified_net_total=req.verified_net_total,
        header_verified=req.header_verified,
        invoice_date=req.invoice_date.isoformat(),
        invoice_no=req.invoice_no,
        currency=req.currency,
        status=BillStatus.APPROVED,
        reason=None,
    )

    updated_bill = store.get_bill(bill_id)
    lines = store.get_review_lines(bill_id)
    return _bill_to_response(updated_bill, lines=lines)


@router.post("/bills/{bill_id}/reject", response_model=BillResponse)
async def reject_bill(
    bill_id: str,
    req: RejectRequest,
    store: BaseStore = Depends(get_store),
) -> BillResponse:
    """Reject a bill permanently with a specified reason."""
    bill = store.get_bill(bill_id)
    if bill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Bill '{bill_id}' not found")

    store.update_bill(
        bill_id,
        reviewer=req.reviewer,
        reason=req.reason,
        status=BillStatus.REJECTED,
    )

    updated_bill = store.get_bill(bill_id)
    lines = store.get_review_lines(bill_id)
    return _bill_to_response(updated_bill, lines=lines)


# ---------------------------------------------------------------------------
# Worker Triggers (Manual / Scheduled)
# ---------------------------------------------------------------------------

@router.post("/workers/run/{stage}")
async def trigger_worker(
    stage: str,
    engine: WorkflowEngine = Depends(get_engine),
) -> dict:
    """
    Manually trigger one execution cycle of a specific pipeline worker.
    Supported stages:
      - 'scan'  : pick oldest QUEUED bill and run Gemini extraction
      - 'post'  : pick oldest APPROVED bill, validate & post purchases/alerts
      - 'alert' : pick oldest PENDING alert and dispatch via WhatsApp
    """
    normalized_stage = stage.lower().strip()
    if normalized_stage == "scan":
        processed_id = await engine.run_scan_worker()
        return {"stage": "scan", "processed_bill_id": processed_id}
    elif normalized_stage == "post":
        processed_id = await engine.run_post_worker()
        return {"stage": "post", "processed_bill_id": processed_id}
    elif normalized_stage == "alert":
        processed_id = await engine.run_alert_worker()
        return {"stage": "alert", "processed_alert_id": processed_id}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown worker stage '{stage}'. Supported stages: 'scan', 'post', 'alert'",
        )


@router.get("/workers/status")
async def get_workers_status(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Check the status of automated background workers and intervals."""
    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_running = scheduler.running if scheduler else False
    jobs_info = []
    if scheduler and scheduler_running:
        for job in scheduler.get_jobs():
            jobs_info.append({
                "id": job.id,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            })
    return {
        "background_workers_enabled": settings.enable_background_workers,
        "scheduler_running": scheduler_running,
        "intervals": {
            "scan_seconds": settings.worker_scan_interval_seconds,
            "post_seconds": settings.worker_post_interval_seconds,
            "alert_seconds": settings.worker_alert_interval_seconds,
        },
        "jobs": jobs_info,
    }
