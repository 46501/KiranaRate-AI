"""
tests/test_api.py
=================
Tests for FastAPI application endpoints:
- GET /health
- GET /webhook/whatsapp (Hub verification challenge)
- POST /webhook/whatsapp (Inbound payload processing)
- GET /bills (listing & status filtering)
- GET /bills/{bill_id} (detail with lines)
- POST /bills/{bill_id}/approve (reviewer approval)
- POST /bills/{bill_id}/reject (reviewer rejection)
- POST /workers/run/{stage} (manual worker triggers)
- GET / and GET /ui/bills (HTML dashboard)
- GET /ui/bills/{bill_id} (HTML review page)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.models import BillStatus, HeaderVerified, Reviewed


class TestApiEndpoints:
    def test_health_check(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "timestamp" in data

    def test_whatsapp_webhook_verification_success(self, client: TestClient):
        # Retrieve configured verify token
        token = app.state.settings.whatsapp_verify_token
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": token,
                "hub.challenge": "CHALLENGE_STRING_12345",
            },
        )
        assert response.status_code == 200
        assert response.text == "CHALLENGE_STRING_12345"

    def test_whatsapp_webhook_verification_failure(self, client: TestClient):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "WRONG_TOKEN",
                "hub.challenge": "CHALLENGE_STRING_12345",
            },
        )
        assert response.status_code == 403

    def test_whatsapp_webhook_intake_post(self, client: TestClient, sample_webhook_factory):
        payload = sample_webhook_factory(message_id="wamid.API_TEST_01")
        response = client.post("/webhook/whatsapp", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_list_and_get_bills(self, client: TestClient, make_bill, make_review_line):
        bill = make_bill(bill_id="bill_api_test_list", status=BillStatus.REVIEW)
        make_review_line(bill_id=bill.bill_id, line_no=1)

        # List all
        resp = client.get("/bills")
        assert resp.status_code == 200
        bills = resp.json()
        assert any(b["bill_id"] == "bill_api_test_list" for b in bills)

        # Filter by status
        resp_filtered = client.get("/bills", params={"status": "REVIEW"})
        assert resp_filtered.status_code == 200
        assert all(b["status"] == "REVIEW" for b in resp_filtered.json())

        # Get detail
        resp_detail = client.get(f"/bills/{bill.bill_id}")
        assert resp_detail.status_code == 200
        detail = resp_detail.json()
        assert detail["bill_id"] == bill.bill_id
        assert len(detail["lines"]) == 1

    def test_approve_bill_endpoint(self, client: TestClient, make_bill, make_review_line):
        bill = make_bill(bill_id="bill_api_approve", status=BillStatus.REVIEW)
        ln = make_review_line(bill_id=bill.bill_id, line_no=1)

        payload = {
            "reviewer": "test_approver",
            "supplier_id": "SUP001",
            "cost_basis": "EX_GST",
            "verified_net_total": "460.00",
            "header_verified": "YES",
            "invoice_date": "2026-11-20",
            "invoice_no": "INV-API-APP",
            "currency": "INR",
            "line_updates": [
                {
                    "review_id": ln.review_id,
                    "sku": "BISCUIT-PARLEG-10",
                    "received_units": 10,
                    "net_amount": "460.00",
                    "reviewed": "YES",
                }
            ],
        }

        resp = client.post(f"/bills/{bill.bill_id}/approve", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "APPROVED"
        assert data["reviewer"] == "test_approver"
        assert data["supplier_id"] == "SUP001"

    def test_reject_bill_endpoint(self, client: TestClient, make_bill):
        bill = make_bill(bill_id="bill_api_reject", status=BillStatus.REVIEW)
        resp = client.post(
            f"/bills/{bill.bill_id}/reject",
            json={"reviewer": "test_rejecter", "reason": "Blurred receipt"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "REJECTED"
        assert data["reason"] == "Blurred receipt"

    def test_trigger_worker_endpoints(self, client: TestClient):
        # Scan trigger
        resp_scan = client.post("/workers/run/scan")
        assert resp_scan.status_code == 200
        assert "stage" in resp_scan.json()

        # Post trigger
        resp_post = client.post("/workers/run/post")
        assert resp_post.status_code == 200

        # Alert trigger
        resp_alert = client.post("/workers/run/alert")
        assert resp_alert.status_code == 200

        # Invalid stage
        resp_bad = client.post("/workers/run/invalid_stage")
        assert resp_bad.status_code == 400

    def test_workers_status_endpoint(self, client: TestClient):
        resp = client.get("/workers/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "background_workers_enabled" in data
        assert "scheduler_running" in data
        assert "intervals" in data
        assert data["intervals"]["scan_seconds"] == 15
        assert data["intervals"]["post_seconds"] == 15
        assert data["intervals"]["alert_seconds"] == 30

    def test_ui_dashboard_renders_html(self, client: TestClient):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "AI Bill Scanner Dashboard" in resp.text

        resp_bills = client.get("/ui/bills")
        assert resp_bills.status_code == 200
        assert "AI Bill Scanner Dashboard" in resp_bills.text

    def test_ui_bill_detail_renders_html(self, client: TestClient, make_bill):
        bill = make_bill(bill_id="bill_ui_detail_test")
        resp = client.get(f"/ui/bills/{bill.bill_id}")
        assert resp.status_code == 200
        assert "Bill Review" in resp.text
        assert bill.bill_id in resp.text
