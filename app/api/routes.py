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

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
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
from app.services.accounting_export import AccountingExporter
from app.services.sku_matcher import SKUMatcher
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


@router.post("/bills/upload")
async def upload_bill(
    file: UploadFile = File(...),
    sender: str = Query("web-upload", description="Sender label or phone number"),
    engine: WorkflowEngine = Depends(get_engine),
) -> dict:
    """
    Directly upload an invoice image (JPEG or PNG) to queue it for AI scanning.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    try:
        bill_id = await engine.intake_uploaded_bill(
            image_bytes=content,
            filename=file.filename or "upload.jpg",
            mime_type=file.content_type or "image/jpeg",
            sender=sender,
        )
        return {
            "status": "success",
            "bill_id": bill_id,
            "message": "Bill uploaded successfully and queued for AI scan.",
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to intake uploaded bill: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to process uploaded bill")


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


@router.post("/bills/{bill_id}/quick-approve", response_model=BillResponse)
async def quick_approve_bill(
    bill_id: str,
    store: BaseStore = Depends(get_store),
) -> BillResponse:
    """
    1-Click Quick Approval endpoint: automatically applies suggested SKUs,
    validates line amounts, marks header verified, and sets status to APPROVED.
    """
    bill = store.get_bill(bill_id)
    if not bill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Bill '{bill_id}' not found")
    if bill.status != BillStatus.REVIEW:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Bill is in {bill.status.value} state, must be in REVIEW")

    lines = store.get_review_lines(bill_id)
    if not lines:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot quick-approve bill with 0 lines")

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

    products = store.list_products(active_only=True)
    from decimal import Decimal
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

    from datetime import date
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

    updated_bill = store.get_bill(bill_id)
    updated_lines = store.get_review_lines(bill_id)
    return _bill_to_response(updated_bill, lines=updated_lines)



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


# ---------------------------------------------------------------------------
# SKU Matching & Intelligence
# ---------------------------------------------------------------------------

@router.get("/sku/suggest")
async def suggest_sku(
    q: str = Query(..., description="Raw product description string from OCR"),
    pack: Optional[str] = Query(None, description="Raw pack size or unit string"),
    min_confidence: float = Query(0.35, ge=0.0, le=1.0, description="Minimum confidence threshold"),
    store: BaseStore = Depends(get_store),
) -> list[dict]:
    """
    Intelligent fuzzy SKU recommendation for invoice line items.
    """
    products = store.list_products(active_only=True)
    matches = SKUMatcher.match(q, pack, products, min_confidence=min_confidence)
    return [
        {
            "sku": m.sku,
            "product_name": m.product_name,
            "confidence": m.confidence,
            "confidence_percent": m.confidence_percent,
            "matched_pack": m.matched_pack,
        }
        for m in matches[:5]
    ]


# ---------------------------------------------------------------------------
# Purchase Price Analytics & Intelligence
# ---------------------------------------------------------------------------

@router.get("/analytics/sku-history")
async def get_sku_price_history(
    sku: str = Query(..., description="Canonical SKU identifier"),
    store: BaseStore = Depends(get_store),
) -> dict:
    """
    Historical purchase rate movement, volatility, and supplier breakdown for an SKU.
    """
    product = store.get_product(sku)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"SKU '{sku}' not found")

    purchases = store.get_purchases_for_sku(sku)
    suppliers_map = {s.supplier_id: s.supplier_name for s in store.list_suppliers()}

    if not purchases:
        return {
            "sku": sku,
            "product_name": product.product_name,
            "purchase_count": 0,
            "current_rate_inr": None,
            "min_rate_inr": None,
            "max_rate_inr": None,
            "avg_rate_inr": None,
            "volatility_percent": 0.0,
            "total_units": 0,
            "history": [],
        }

    rates_inr = [round(p.net_paise / (p.units * 100), 2) for p in purchases if p.units > 0]
    total_units = sum(p.units for p in purchases)
    min_rate = min(rates_inr) if rates_inr else 0.0
    max_rate = max(rates_inr) if rates_inr else 0.0
    avg_rate = round(sum(rates_inr) / len(rates_inr), 2) if rates_inr else 0.0
    latest_rate = rates_inr[-1] if rates_inr else 0.0
    volatility = round(((max_rate - min_rate) / min_rate) * 100, 2) if min_rate > 0 else 0.0

    history = [
        {
            "purchase_id": p.purchase_id,
            "bill_id": p.bill_id,
            "invoice_date": p.invoice_date.isoformat(),
            "supplier_id": p.supplier_id,
            "supplier_name": suppliers_map.get(p.supplier_id, p.supplier_id),
            "units": p.units,
            "rate_inr": round(p.net_paise / (p.units * 100), 2) if p.units > 0 else 0.0,
            "net_amount_inr": round(p.net_paise / 100, 2),
            "posted_at": p.posted_at.isoformat(),
        }
        for p in purchases
    ]

    return {
        "sku": sku,
        "product_name": product.product_name,
        "purchase_count": len(purchases),
        "current_rate_inr": latest_rate,
        "min_rate_inr": min_rate,
        "max_rate_inr": max_rate,
        "avg_rate_inr": avg_rate,
        "volatility_percent": volatility,
        "total_units": total_units,
        "history": history,
    }


@router.get("/analytics/summary")
async def get_analytics_summary(
    store: BaseStore = Depends(get_store),
) -> list[dict]:
    """
    Summary rate metrics across all products with posted purchase history.
    """
    products = store.list_products(active_only=True)
    summary = []
    for prod in products:
        purchases = store.get_purchases_for_sku(prod.sku)
        if purchases:
            rates_inr = [round(p.net_paise / (p.units * 100), 2) for p in purchases if p.units > 0]
            min_rate = min(rates_inr)
            max_rate = max(rates_inr)
            avg_rate = round(sum(rates_inr) / len(rates_inr), 2)
            volatility = round(((max_rate - min_rate) / min_rate) * 100, 2) if min_rate > 0 else 0.0
            summary.append({
                "sku": prod.sku,
                "product_name": prod.product_name,
                "purchases_count": len(purchases),
                "latest_rate_inr": rates_inr[-1],
                "min_rate_inr": min_rate,
                "max_rate_inr": max_rate,
                "avg_rate_inr": avg_rate,
                "volatility_percent": volatility,
            })
    return summary


# ---------------------------------------------------------------------------
# Accounting & ERP Exports (Excel CSV & Tally XML)
# ---------------------------------------------------------------------------

@router.get("/bills/export/csv")
@router.get("/export/csv")
async def export_purchases_csv(
    store: BaseStore = Depends(get_store),
) -> Response:
    """Export purchase transactions to CSV for Excel / Google Sheets."""
    csv_data = AccountingExporter.generate_csv(store)
    filename = f"KiranaIQ_Purchases_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/bills/export/tally")
@router.get("/export/tally")
async def export_tally_xml(
    store: BaseStore = Depends(get_store),
) -> Response:
    """Export posted purchase vouchers formatted for direct Tally Prime XML import."""
    xml_data = AccountingExporter.generate_tally_xml(store)
    filename = f"KiranaIQ_Tally_Vouchers_{datetime.now(timezone.utc).strftime('%Y%m%d')}.xml"
    return Response(
        content=xml_data,
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )



