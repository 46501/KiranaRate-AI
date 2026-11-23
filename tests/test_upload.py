"""
tests/test_upload.py
====================
Integration tests for direct invoice file upload and AI scanning.
"""

import io
import pytest
from fastapi.testclient import TestClient
from app.models import BillStatus
from app.services.workflow_engine import WorkflowEngine


def test_upload_bill_endpoint_success(client: TestClient, engine: WorkflowEngine):
    fake_image = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100
    files = {"file": ("test_invoice.jpg", io.BytesIO(fake_image), "image/jpeg")}

    resp = client.post("/bills/upload", files=files)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "bill_id" in data
    assert data["bill_id"].startswith("wamid.WEB_")

    # Verify bill is present in store and QUEUED
    bill = engine._store.get_bill(data["bill_id"])
    assert bill is not None
    assert bill.status == BillStatus.QUEUED
    assert bill.mime_type == "image/jpeg"


def test_upload_bill_endpoint_rejects_empty(client: TestClient):
    files = {"file": ("empty.jpg", io.BytesIO(b""), "image/jpeg")}
    resp = client.post("/bills/upload", files=files)
    assert resp.status_code == 400


def test_upload_bill_endpoint_rejects_invalid_mime(client: TestClient):
    files = {"file": ("doc.txt", io.BytesIO(b"Hello text file"), "text/plain")}
    resp = client.post("/bills/upload", files=files)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_scan_worker_processes_uploaded_local_bill(engine: WorkflowEngine):
    fake_image = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 200
    bill_id = await engine.intake_uploaded_bill(
        image_bytes=fake_image,
        filename="local_scan_test.jpg",
        mime_type="image/jpeg",
        sender="web-test",
    )

    bill = engine._store.get_bill(bill_id)
    assert bill.status == BillStatus.QUEUED

    # Run scan worker
    scanned_id = await engine.run_scan_worker()
    assert scanned_id == bill_id

    # Verify transition to REVIEW
    updated_bill = engine._store.get_bill(bill_id)
    assert updated_bill.status == BillStatus.REVIEW
    lines = engine._store.get_review_lines(bill_id)
    assert len(lines) > 0
