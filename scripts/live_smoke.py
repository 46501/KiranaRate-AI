"""
scripts/live_smoke.py
=====================
Command-line end-to-end verification and live integration probe for AI Bill Scanner.

Usage:
  # 1. Run complete offline simulation (uses temporary database and mocks):
  python scripts/live_smoke.py

  # 2. Run live integration probe (checks real credentials in .env and tests connectivity):
  python scripts/live_smoke.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from app.config import Settings, settings
from app.models import (
    AlertStatus,
    BillStatus,
    CostBasis,
    HeaderVerified,
    Product,
    ProductActive,
    Purchase,
    Reviewed,
    Supplier,
    SupplierActive,
)
from app.schemas import AlertParams
from app.services.gemini_scanner import GeminiScanner, MockGeminiScanner
from app.services.whatsapp import MockWhatsAppClient, WhatsAppClient
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc


async def run_offline_simulation() -> bool:
    """
    Simulate full cycle:
    WhatsApp Intake Webhook -> Gemini AI Scan -> Human Review -> Post & Compare -> WhatsApp Alert Outbox.
    Runs on an isolated temporary SQLite database to avoid polluting local bills.db.
    """
    print("\n" + "=" * 70)
    print("AI BILL SCANNER — FULL PIPELINE END-TO-END SIMULATION")
    print("=" * 70)

    # 1. Create temporary isolated SQLite database
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        temp_db = Path(tmp_dir) / "smoke_test.db"
        print(f"[Setup] Initializing isolated temporary store at {temp_db}...")

        sim_settings = Settings(
            sqlite_db_path=str(temp_db),
            mock_whatsapp=True,
            mock_gemini=True,
            whatsapp_phone_number_id="100200300",
            whatsapp_owner_number="919876543210",
            whatsapp_allowlist=["919876543210"],
            whatsapp_template_name="purchase_rate_review",
            whatsapp_template_lang="en",
            alert_lookback_days=30,
            alert_new_bill_days=7,
            alert_min_gap_pct=2.00,
            alert_min_gap_paise=1,
        )

        store = SQLiteStore(str(temp_db))
        store.create_tables()

        # Seed master suppliers & products
        store.upsert_supplier(Supplier(supplier_id="SUP001", supplier_name="Ramesh Traders", active=SupplierActive.YES))
        store.upsert_supplier(Supplier(supplier_id="SUP002", supplier_name="Suresh Wholesale", active=SupplierActive.YES))
        store.upsert_product(Product(sku="BISCUIT-PARLEG-10", product_name="Parle-G Biscuit 10pk", base_unit="PACK", active=ProductActive.YES))

        # Pre-seed historical comparison purchase (5 days ago from Suresh Wholesale @ Rs 46.00 = 4600 paise/unit)
        hist_date = date.today() - timedelta(days=5)
        store.insert_purchase(
            Purchase(
                purchase_id="purch_hist_smoke",
                bill_id="bill_hist_smoke",
                sku="BISCUIT-PARLEG-10",
                supplier_id="SUP002",
                invoice_no="INV-HIST-46",
                invoice_date=hist_date,
                cost_basis=CostBasis.EX_GST,
                units=10,
                net_paise=46000,
                posted_at=datetime.now(UTC) - timedelta(days=5),
            )
        )
        print("  ✓ Seeded Suppliers (Ramesh Traders, Suresh Wholesale)")
        print("  ✓ Seeded Products (BISCUIT-PARLEG-10)")
        print(f"  ✓ Pre-seeded Historical Purchase: Suresh Wholesale @ 4600 paise/unit on {hist_date}")

        # Setup mock clients & engine
        mock_gemini = MockGeminiScanner(
            fixture_response={
                "supplier_raw": "Ramesh Traders",
                "invoice_no": "INV-SMOKE-500",
                "invoice_date": date.today().isoformat(),
                "currency": "INR",
                "invoice_total": "500.00",
                "notes": "Clear scanned bill image",
                "lines": [
                    {
                        "line_no": 1,
                        "product_raw": "Parle-G 10pk Biscuit",
                        "pack_raw": "10pk",
                        "quantity_raw": "10",
                        "unit_raw": "PACK",
                        "rate_raw": "50.00",
                        "line_total_raw": "500.00",
                    }
                ],
            }
        )
        mock_whatsapp = MockWhatsAppClient(
            template_name=sim_settings.whatsapp_template_name,
            template_lang=sim_settings.whatsapp_template_lang,
        )
        engine = WorkflowEngine(
            store=store,
            scanner=mock_gemini,
            whatsapp=mock_whatsapp,
            settings=sim_settings,
        )

        # ── Pipeline 1: Intake Webhook ──────────────────────────────
        print("\n[Pipeline 1] Simulating Inbound WhatsApp Webhook...")
        webhook_payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "ENTRY_001",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "1234567890",
                                    "phone_number_id": "100200300",
                                },
                                "messages": [
                                    {
                                        "id": "wamid.SMOKE_TEST_MSG_001",
                                        "from": "919876543210",
                                        "timestamp": str(int(datetime.now(UTC).timestamp())),
                                        "type": "image",
                                        "image": {
                                            "id": "media_smoke_001",
                                            "mime_type": "image/jpeg",
                                        },
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

        inserted = await engine.run_intake(webhook_payload)
        assert len(inserted) == 1, "Expected 1 bill inserted"
        bill_id = inserted[0]
        queued_bill = store.get_bill(bill_id)
        assert queued_bill.status == BillStatus.QUEUED
        print(f"  ✓ Bill Intake Success: bill_id={bill_id} status={queued_bill.status.value}")

        # ── Pipeline 2: AI Scan Worker ──────────────────────────────
        print("\n[Pipeline 2] Simulating AI Scan Worker (Gemini Vision)...")
        scanned_id = await engine.run_scan_worker()
        assert scanned_id == bill_id
        reviewed_bill = store.get_bill(bill_id)
        assert reviewed_bill.status == BillStatus.REVIEW
        review_lines = store.get_review_lines(bill_id)
        assert len(review_lines) == 1
        print(f"  ✓ Gemini Scan Success: extracted {len(review_lines)} line item(s)")
        print(f"    - Raw item: {review_lines[0].product_raw} | Rate: Rs {review_lines[0].rate_raw} | Line Total: Rs {review_lines[0].line_total_raw}")
        print(f"    - Transitioned to status={reviewed_bill.status.value}")

        # ── Human Review Step ───────────────────────────────────────
        print("\n[Human Review] Simulating Reviewer Verification & SKU Mapping...")
        store.update_review_line(
            review_lines[0].review_id,
            sku="BISCUIT-PARLEG-10",
            received_units=10,
            net_amount=Decimal("500.00"),
            reviewed=Reviewed.YES,
        )
        store.update_bill(
            bill_id,
            reviewer="qa_manager",
            supplier_id="SUP001",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("500.00"),
            header_verified=HeaderVerified.YES,
            invoice_date=date.today().isoformat(),
            invoice_no="INV-SMOKE-500",
            currency="INR",
            status=BillStatus.APPROVED,
        )
        approved_bill = store.get_bill(bill_id)
        assert approved_bill.status == BillStatus.APPROVED
        print(f"  ✓ Review Completed: header verified, mapped to 'BISCUIT-PARLEG-10', status={approved_bill.status.value}")

        # ── Pipeline 3: Verify, Post & Rate Comparison ──────────────
        print("\n[Pipeline 3] Simulating Verify, Post & Rate Comparison...")
        posted_id = await engine.run_post_worker()
        assert posted_id == bill_id
        posted_bill = store.get_bill(bill_id)
        assert posted_bill.status == BillStatus.POSTED
        purchases = store.get_purchases_by_bill(bill_id)
        assert len(purchases) == 1
        print(f"  ✓ Purchase Ledger Updated: bill_id={bill_id} status={posted_bill.status.value}")
        print(f"    - Recorded: {purchases[0].sku} @ {purchases[0].net_paise // purchases[0].units} paise/unit")

        # Rate comparison check: 5000 paise vs 4600 paise -> 8.00% gap
        alert = store.get_oldest_pending_alert()
        assert alert is not None, "Alert should have been generated!"
        print(f"  ✓ Price Gap Detected: Low=4600 paise (Suresh Wholesale) vs High=5000 paise (Ramesh Traders)")
        print(f"    - Gap: {alert.high_rate - alert.low_rate} paise | Percentage: {alert.gap_pct:.2f}% (Threshold: 2.00%)")
        print(f"    - PENDING Alert created: alert_id={alert.alert_id}")

        # ── Pipeline 4: Price Alert Outbox Worker ───────────────────
        print("\n[Pipeline 4] Simulating WhatsApp Alert Outbox Dispatch...")
        alert_id = await engine.run_alert_worker()
        assert alert_id == alert.alert_id
        assert len(mock_whatsapp.sent_alerts) == 1

        sent_record = mock_whatsapp.sent_alerts[0]
        params = sent_record.params
        print(f"  ✓ WhatsApp Template Message Dispatched!")
        print(f"    - Recipient: {sent_record.to_number}")
        print(f"    - Template: {sent_record.template_name} ({sent_record.template_lang})")
        print(f"    - Returned WAMID: {sent_record.wamid}")
        print("    - 10 Template Parameters:")
        param_labels = [
            "1. SKU",
            "2. Low Supplier",
            "3. Low Rate",
            "4. Low Date",
            "5. Low Bill ID",
            "6. High Supplier",
            "7. High Rate",
            "8. High Date",
            "9. High Bill ID",
            "10. Gap Percentage",
        ]
        for label, val in zip(param_labels, params):
            print(f"      {label:<18}: {val}")

        # Verify Alert state
        assert store.get_oldest_pending_alert() is None

        # Cleanly close SQLite store to release file locks on Windows
        store.close()

    print("\n" + "=" * 70)
    print("ALL 4 PIPELINES COMPLETED FLAWLESSLY IN DRY-RUN SIMULATION (PASS)")
    print("=" * 70 + "\n")
    return True


def _is_placeholder(val: str | None) -> bool:
    if not val:
        return True
    s = str(val).strip().lower()
    return s.startswith("your_") or s in ("changeme", "none", "placeholder", "")


async def run_live_connectivity_probe() -> bool:
    """
    Probe real credentials configured in .env for live external readiness:
      1. Gemini API key validation
      2. Meta WhatsApp Cloud API credentials and Phone Number ID probe
      3. WhatsApp Webhook and Template inspection
    """
    print("\n" + "=" * 70)
    print("AI BILL SCANNER — LIVE INTEGRATION READINESS PROBE")
    print("=" * 70)

    cfg = settings
    all_ok = True

    # ── 1. Check Gemini API ─────────────────────────────────────
    print("\n[1/3] Checking Google Gemini Vision Configuration...")
    if _is_placeholder(cfg.gemini_api_key):
        print("  ⚠️  GEMINI_API_KEY is not configured or using placeholder.")
        print("     To activate live scanning, get an API key at https://aistudio.google.com/")
        print("     and set GEMINI_API_KEY=AIza... in .env (MOCK_GEMINI=false).")
        gemini_live = False
    else:
        print(f"  ✓ GEMINI_API_KEY detected (Length: {len(cfg.gemini_api_key)})")
        print(f"  ✓ Model: {cfg.gemini_model}")
        try:
            import google.generativeai as genai
            genai.configure(api_key=cfg.gemini_api_key)
            model = genai.GenerativeModel(cfg.gemini_model)
            # Lightweight test call
            print("  ... Probing Gemini API with quick ping...")
            resp = await model.generate_content_async("Ping. Reply with 'PONG' only.")
            print(f"  ✓ Gemini API Connectivity PASS: Response={resp.text.strip()[:30]!r}")
            gemini_live = True
        except Exception as exc:
            print(f"  ✕ Gemini API Probe FAILED: {exc}")
            all_ok = False
            gemini_live = False

    # ── 2. Check Meta WhatsApp Cloud API ────────────────────────
    print("\n[2/3] Checking Meta WhatsApp Cloud API Configuration...")
    wa_token = cfg.whatsapp_access_token
    phone_id = cfg.whatsapp_phone_number_id

    if _is_placeholder(wa_token) or _is_placeholder(phone_id):
        print("  ⚠️  WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID not configured.")
        print("     To activate live WhatsApp messaging, get a System User Token and Phone Number ID")
        print("     from Meta for Developers (https://developers.facebook.com/) and set them in .env.")
        whatsapp_live = False
    else:
        print(f"  ✓ WHATSAPP_ACCESS_TOKEN detected (Length: {len(wa_token)})")
        print(f"  ✓ WHATSAPP_PHONE_NUMBER_ID: {phone_id}")
        print(f"  ✓ API Version: {cfg.whatsapp_api_version}")
        print(f"  ✓ Owner Number (Alerts Recipient): {cfg.whatsapp_owner_number or 'NOT SET'}")
        print(f"  ✓ Allowlist Senders: {cfg.whatsapp_allowlist or 'EMPTY'}")

        # Graph API probe
        url = f"https://graph.facebook.com/{cfg.whatsapp_api_version}/{phone_id}"
        headers = {"Authorization": f"Bearer {wa_token}"}
        try:
            print(f"  ... Probing Meta Graph API endpoint: {url}...")
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    display_phone = data.get("display_phone_number", "N/A")
                    verified_name = data.get("verified_name", "N/A")
                    print(f"  ✓ Meta Graph API Probe PASS: Phone={display_phone} Name={verified_name}")
                    whatsapp_live = True
                else:
                    print(f"  ✕ Meta Graph API Probe FAILED: HTTP {res.status_code} - {res.text}")
                    all_ok = False
                    whatsapp_live = False
        except Exception as exc:
            print(f"  ✕ Meta Graph API Connection Error: {exc}")
            all_ok = False
            whatsapp_live = False

    # ── 3. Check WhatsApp Template & Webhook ────────────────────
    print("\n[3/3] Checking WhatsApp Template & Webhook Settings...")
    print(f"  ✓ Template Name: {cfg.whatsapp_template_name}")
    print(f"  ✓ Template Language: {cfg.whatsapp_template_lang}")
    print(f"  ✓ Webhook Verify Token: {cfg.whatsapp_verify_token}")
    print(f"  ✓ Mock Toggles: MOCK_WHATSAPP={cfg.mock_whatsapp}, MOCK_GEMINI={cfg.mock_gemini}, DRY_RUN={cfg.dry_run}")

    print("\n" + "=" * 70)
    print("LIVE READINESS SUMMARY")
    print("=" * 70)
    print(f"  Google Gemini Vision Live  : {'READY (PASS)' if gemini_live else 'MOCK / NOT CONFIGURED'}")
    print(f"  Meta WhatsApp Cloud API Live: {'READY (PASS)' if whatsapp_live else 'MOCK / NOT CONFIGURED'}")
    print(f"  Overall Live Readiness     : {'READY FOR LIVE PROD' if (gemini_live and whatsapp_live) else 'SAFE LOCAL MOCK MODE ACTIVE'}")
    print("=" * 70 + "\n")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="AI Bill Scanner Live Readiness & Simulation Tool")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live connectivity probe against configured external APIs in .env",
    )
    args = parser.parse_args()

    if args.live:
        success = asyncio.run(run_live_connectivity_probe())
    else:
        success = asyncio.run(run_offline_simulation())

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
