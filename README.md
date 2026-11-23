# KiranaIQ — AI-Powered Kirana Purchase Intelligence & Bill Automation

A production-ready prototype and enterprise-grade procurement intelligence system for Indian kirana / retail stores. Built with **Python + FastAPI + SQLite + Google Gemini Vision + WhatsApp Cloud API (Option 2 Architecture)**.

The system automates WhatsApp bill intake, AI invoice extraction, human reviewer verification, purchase price gap monitoring against 30-day supplier history, and automated WhatsApp price discrepancy alerts — while strictly enforcing Indian GST standards, Indian Financial Year boundaries, integer paise math (`net_paise // units`), and zero floating-point drift.

---

## 1. System Architecture & Core Pipelines

```
[Store / Kirana Supplier]
           │
           │  Sends bill image via WhatsApp (or drag-and-drop web upload)
           ▼
[Pipeline 1: WhatsApp Intake Webhook]
   ├── Validates webhook challenge & verification token (hub.verify_token)
   ├── Filters: phone_number_id & allowlisted sender E.164
   ├── Deduplicates by message.id (bill_id)
   ├── Saves raw image to disk (media/)
   └── Inserts Bills table (status: QUEUED)
           │
           ▼
[Pipeline 2: AI Scan Worker (Gemini Vision)]
   ├── Picks oldest QUEUED bill -> status: SCANNING
   ├── Downloads media (validates JPEG/PNG <= 5 MiB)
   ├── Calls Gemini Vision with strict JSON output schema & prompt guardrails
   ├── Enforces 1..75 line items validation
   ├── Writes ReviewLines records
   └── Transitions bill -> status: REVIEW
           │
           ▼
[Human Review Interface (KiranaIQ Web Workspace)]
   ├── Reviewer inspects side-by-side: original invoice image vs. extracted lines
   ├── Verifies header (Supplier, Invoice No, Date, Net Total, INR, EX_GST)
   ├── Canonical SKU mapping (with explicit suggestions, no silent auto-merges)
   ├── Adjusts received units (bonus/free pack normalization)
   ├── Real-time financial reconciliation strip with 1-click paise balancing
   ├── 1-Click "Quick Approve" workflow for verified bills
   └── Submits approval -> status: APPROVED (or REJECTED)
           │
           ▼
[Pipeline 3: Verify, Post & Rate Comparison Worker]
   ├── Validates all 15 business rules (Indian FY duplicates, line counts, paise sums)
   ├── If invalid -> moves back to REVIEW with descriptive reason
   ├── Aggregates line items per SKU (units & integer net_paise)
   ├── Upserts Purchases ledger records
   ├── Runs Rate Comparison against last 30-day history (EX_GST):
   │     ├── Condition 1: Price gap >= 1 paisa per unit
   │     └── Condition 2: Percentage gap >= 2.00% (100 * (high - low) / high)
   ├── Generates Alerts record (status: PENDING) for qualifying bills
   └── Transitions bill -> status: POSTED
           │
           ▼
[Pipeline 4: Price Alert Outbox Worker]
   ├── Picks oldest PENDING alert (parent bill must be POSTED)
   ├── Expires alerts older than 24 hours -> status: EXPIRED
   ├── Marks status: SENDING with attempted_at before network call
   ├── Dispatches WhatsApp template 'purchase_rate_review' with 10 parameters
   └── On wamid receipt -> marks status: SENT with wamid & sent_at
```

---

## 2. Business Rules & Financial Precision

1. **Integer Paise Normalization:**
   - 1 INR = 100 paise.
   - All purchase rates, totals, and unit comparisons are computed and stored in integer paise (`net_paise // units`), eliminating floating-point drift and rounding discrepancies.
2. **Indian Financial Year Boundaries:**
   - Indian FY runs from April 1 to March 31 of the subsequent year.
   - Duplicate invoice check prevents duplicate `(supplier_id, Indian_FY, invoice_no)` among `POSTED` bills. An invoice with the same number in a different FY is permitted.
3. **SKU & Pack Isolation:**
   - Different pack sizes or variants are intentionally distinct SKUs (e.g. 10pk vs 20pk are distinct SKUs and never silently auto-merged).
4. **Bonus & Free Pack Normalization:**
   - Reviewer enters actual `received_units`. If a retailer buys 10 boxes and receives 1 free box (total 11 boxes) for Rs 500, effective per-unit rate is normalized as `50000 // 11 = 4545 paise/unit`.
5. **Comparison & Alert Trigger Conditions:**
   - **Lookback Window:** Compares against the cheapest active other supplier's rate in the last **30 days**.
   - **New Bill Gate:** Only bills with `invoice_date` within the last **7 days** (relative to `Asia/Kolkata` today) are eligible to trigger alerts.
   - **Alert Thresholds:** Triggered only if `gap_paise >= 1` AND `gap_pct >= 2.00%` (`100 * (high_rate - low_rate) / high_rate >= 2.0`).
6. **Outbox Safety & Idempotency:**
   - Alert status transitions to `SENDING` *before* the WhatsApp API call.
   - In case of network timeout or delivery error, the alert remains in `SENDING` and is **never blindly retried** automatically, preventing duplicate alert spam to store owners.

---

## 3. Advanced Features & SaaS Capabilities

- **Interactive Visual Price Trend Modal (Chart.js):**
  - Click **View Price Chart** on any SKU card to open a modal with dynamic min/max/current rates, volatility percentage, interactive line chart, and historical purchase table.
- **1-Click "Quick Approve" Workflow:**
  - One-click approval button (`POST /bills/{bill_id}/quick-approve`) on the dashboard for verified bills, automatically balancing paise, resolving canonical SKUs, and triggering the posting worker.
- **Indian GST Slabs & GSTR-2B Input Tax Credit (ITC) Ledger:**
  - Standard Indian FMCG tax slabs (0%, 5%, 12%, 18%), 50-50 CGST/SGST breakdown, gross supplier outflow, and supplier-wise GSTR-2B reconciliation (`GET /analytics/gst-itc`).
- **WhatsApp Supplier Negotiation Note Generator:**
  - Generate polite, professional negotiation messages (`GET /alerts/{alert_id}/negotiation-note`) in English and Kirana Hinglish with 1-click WhatsApp deep links (`https://api.whatsapp.com/send?text=...`).
- **Tally Prime & Accounting Exports:**
  - Export purchase transactions directly to Tally Prime XML (`GET /bills/export/tally`) or Excel CSV (`GET /bills/export/csv`).
- **Direct Drag-and-Drop Web Upload:**
  - Upload bill receipts directly from the web dashboard (`POST /ui/upload`) with instant Gemini scanning.

---

## 4. WhatsApp Template Specification

- **Template Name:** `purchase_rate_review` (configurable via `WHATSAPP_TEMPLATE_NAME`)
- **Language:** `en` (configurable via `WHATSAPP_TEMPLATE_LANG`)
- **10 Ordered Body Parameters:**
  1. `{{1}}` — `sku` (Product SKU code, e.g. `BISCUIT-PARLEG-10`)
  2. `{{2}}` — `low_supplier` (Name of cheaper supplier, e.g. `Suresh Wholesale`)
  3. `{{3}}` — `low_rate` (Formatted cheaper rate, e.g. `Rs 46.00/unit`)
  4. `{{4}}` — `low_date` (Date of cheaper purchase, e.g. `15-Nov-2026`)
  5. `{{5}}` — `low_bill` (Bill ID / message ID of cheaper purchase)
  6. `{{6}}` — `high_supplier` (Name of dearer supplier, e.g. `Ramesh Traders`)
  7. `{{7}}` — `high_rate` (Formatted dearer rate, e.g. `Rs 50.00/unit`)
  8. `{{8}}` — `high_date` (Date of dearer purchase, e.g. `20-Nov-2026`)
  9. `{{9}}` — `high_bill` (Bill ID / message ID of dearer purchase)
  10. `{{10}}` — `gap_pct` (Percentage gap, e.g. `8.00%`)

---

## 5. Getting Started Locally

### Prerequisites
- Python 3.10+ (Tested on Python 3.14)
- Virtual environment tool (`venv`)

### Installation & Setup
```bash
# 1. Navigate to project root
cd "d:\AI Bill Scanner"

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate   # On Windows (PowerShell / cmd)
# source .venv/bin/activate # On Linux/macOS

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy environment configuration
copy .env.example .env

# 5. Seed demo database (creates suppliers, products, and sample bills)
python sample_data/seed.py

# 6. Run the FastAPI server with Uvicorn
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

---

## 6. Web Reviewer UI & API Endpoints

Once running, access the local server in your browser:

- **KiranaIQ Dashboard:** [http://127.0.0.1:8000/ui/bills](http://127.0.0.1:8000/ui/bills) (or [http://127.0.0.1:8000/](http://127.0.0.1:8000/))
- **Interactive API Documentation (Swagger):** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Health Check:** `GET http://127.0.0.1:8000/health`
- **Workers Status:** `GET http://127.0.0.1:8000/workers/status`

### Triggering Pipeline Workers via API / CLI
You can execute single worker runs on demand:
```bash
# Run AI scan worker on oldest QUEUED bill
curl -X POST http://127.0.0.1:8000/workers/run/scan

# Run Post & Compare worker on oldest APPROVED bill
curl -X POST http://127.0.0.1:8000/workers/run/post

# Run Alert Outbox worker on oldest PENDING alert
curl -X POST http://127.0.0.1:8000/workers/run/alert
```

---

## 7. End-to-End Simulation & Test Suite

### Full Pipeline Simulation & Live Probe Script
Run the built-in end-to-end verification tool:
```bash
# 1. Run complete offline simulation (creates isolated temporary SQLite DB):
python scripts/live_smoke.py

# 2. Run live integration probe (validates .env keys and tests Gemini/Meta connectivity):
python scripts/live_smoke.py --live
```

### Running Automated Pytest Suite
The automated test suite provides 100% offline coverage with deterministic mocks for Gemini Vision and WhatsApp Cloud API:

```bash
pytest tests -v
```

### Coverage Highlights (76 Tests):
- **Intake Tests (`tests/test_intake.py`):** Challenge verification, allowlist filtering, phone number ID checking, media type enforcement, idempotency/deduplication.
- **Scan Tests (`tests/test_scan.py`):** Gemini JSON extraction, 1..75 line constraints, error handling, note flag preservation.
- **Posting Tests (`tests/test_post.py`):** 15 validation checks, Indian FY invoice duplicate prevention, paise sum reconciliation, SKU/unit validation.
- **Comparison Tests (`tests/test_comparison.py`):** 10 @ Rs 500 vs 10 @ Rs 460 (8.00% gap), same-supplier suppression, 30-day cutoff, 7-day new bill gate, bonus pack unit normalization.
- **Alert Tests (`tests/test_alerts.py`):** Outbox state machine (PENDING -> SENDING -> SENT), 24h expiration, timeout/error freeze, 10 ordered template params.
- **Math Tests (`tests/test_math.py`):** Integer paise conversions, rounding, Indian FY calendar boundary rules.
- **API Tests (`tests/test_api.py`):** FastAPI endpoints, webhook handlers, review/approve/reject routes, HTML template rendering.
- **Live Readiness Tests (`tests/test_live_readiness.py`):** Plus-prefix sender normalization, mixed payload filtering, image size guards, Gemini ambiguous date normalization.
- **Quick Approve Tests (`tests/test_quick_approve.py`):** 1-Click approval API & UI route, auto-matching, posting triggers.
- **GST & Tax Tests (`tests/test_gst.py`):** Indian FMCG GST slabs (0%, 5%, 12%, 18%), CGST/SGST 50-50 splits, ITC claimable ledger.
- **Negotiation Generator Tests (`tests/test_negotiation.py`):** Supplier price negotiation messages (English & Hinglish) and WhatsApp deep-links.

---

## 8. Connecting Live External APIs

To connect live Meta WhatsApp Cloud API and Google Gemini Vision:

1. **Google Gemini API:**
   - Obtain an API key from [Google AI Studio](https://aistudio.google.com/).
   - Set in `.env`:
     ```env
     MOCK_GEMINI=false
     GEMINI_API_KEY=your_actual_gemini_api_key
     GEMINI_MODEL=gemini-1.5-flash
     ```

2. **Meta WhatsApp Cloud API:**
   - Create an app in [Meta for Developers](https://developers.facebook.com/) with WhatsApp product.
   - Configure a webhook pointing to your public URL (via `ngrok http 8000`): `https://your-ngrok-domain.ngrok-free.app/webhook/whatsapp`.
   - Set in `.env`:
     ```env
     MOCK_WHATSAPP=false
     WHATSAPP_VERIFY_TOKEN=your_secure_random_token
     WHATSAPP_ACCESS_TOKEN=your_meta_system_user_access_token
     WHATSAPP_PHONE_NUMBER_ID=your_whatsapp_phone_number_id
     WHATSAPP_OWNER_NUMBER=919876543210
     WHATSAPP_ALLOWLIST=919876543210,919123456789
     ```
   - In Meta Business Manager, create and submit the `purchase_rate_review` template with 10 body variables. Once approved, live alerts will be delivered directly to the store owner's WhatsApp number.

