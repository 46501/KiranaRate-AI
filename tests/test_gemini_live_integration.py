"""
tests/test_gemini_live_integration.py
=====================================
Comprehensive tests for Gemini Vision API live integration and scanner:
- GeminiScanner configuration & placeholder validation
- Mocked API response structured extraction (matching ScanResult schema)
- Date normalization, null-handling, and line count boundaries
- Image size and MIME-type input guards
- Transient vs. permanent error handling & retry backoff
- Timeout handling & JSON parsing error handling
- API key masking in errors and logs
- Preserved MockGeminiScanner backwards compatibility
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import errors

from app.services.gemini_scanner import (
    GeminiScanner,
    MockGeminiScanner,
    ScanResult,
    mask_api_key,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

SAMPLE_INVOICE_JSON = {
    "supplier_raw": "Lakshmi Supermarket",
    "invoice_no": "INV-2026-999",
    "invoice_date": "2026-11-25",
    "currency": "INR",
    "invoice_total": "1250.50",
    "notes": "Clear printed bill",
    "lines": [
        {
            "line_no": 1,
            "product_raw": "Tata Salt 1kg",
            "pack_raw": "1kg",
            "quantity_raw": "10",
            "unit_raw": "PACK",
            "rate_raw": "28.00",
            "line_total_raw": "280.00",
        },
        {
            "line_no": 2,
            "product_raw": "Aashirvaad Atta 5kg",
            "pack_raw": "5kg",
            "quantity_raw": "2",
            "unit_raw": "BAG",
            "rate_raw": "250.00",
            "line_total_raw": "500.00",
        },
    ],
}


def _make_mock_response(text_content: str):
    """Create a mock GenerateContentResponse with .text attribute."""
    resp = MagicMock()
    resp.text = text_content
    resp.candidates = []
    return resp


# ---------------------------------------------------------------------------
# 1. Configuration & Placeholder Validation Tests
# ---------------------------------------------------------------------------

class TestGeminiScannerConfiguration:
    @pytest.mark.parametrize(
        "invalid_key",
        [
            "",
            "   ",
            "your_gemini_api_key_here",
            "changeme",
            "placeholder",
            "none",
            "YOUR_API_KEY",
        ],
    )
    def test_init_rejects_missing_or_placeholder_keys(self, invalid_key: str):
        """GeminiScanner must raise ValueError if key is missing or a placeholder."""
        with pytest.raises(ValueError, match="GEMINI_API_KEY is missing or configured as a placeholder"):
            GeminiScanner(api_key=invalid_key)

    def test_init_accepts_valid_key(self):
        """GeminiScanner successfully initializes with a non-placeholder key and default model."""
        scanner = GeminiScanner(api_key="valid-live-test-key-xyz")
        assert scanner.api_key == "valid-live-test-key-xyz"
        assert scanner.model_name == "gemini-3.8-flash"
        assert scanner._client is None  # Lazy client initialization

    def test_model_override_via_constructor(self):
        """GeminiScanner allows overriding the model name via constructor."""
        scanner = GeminiScanner(api_key="valid-live-test-key-xyz", model_name="gemini-3.5-flash-lite")
        assert scanner.model_name == "gemini-3.5-flash-lite"

    def test_settings_gemini_model_default_and_override(self):
        """Settings defaults to gemini-3.8-flash and respects GEMINI_MODEL override."""
        from app.config import Settings
        # Test class field default directly and with _env_file=None to isolate from local .env
        assert Settings.model_fields["gemini_model"].default == "gemini-3.8-flash"
        cfg_default = Settings(_env_file=None)
        assert cfg_default.gemini_model == "gemini-3.8-flash"

        # Test explicit override
        cfg_custom = Settings(_env_file=None, gemini_model="gemini-3.5-flash-lite")
        assert cfg_custom.gemini_model == "gemini-3.5-flash-lite"

    def test_lazy_client_initialization(self):
        """google.genai.Client is instantiated lazily upon first access."""
        scanner = GeminiScanner(api_key="valid-live-test-key-xyz")
        with patch("app.services.gemini_scanner.genai.Client") as mock_client_cls:
            client = scanner._get_client()
            mock_client_cls.assert_called_once_with(api_key="valid-live-test-key-xyz")
            assert client == mock_client_cls.return_value
            # Second call should reuse cached client
            client2 = scanner._get_client()
            assert client2 is client
            assert mock_client_cls.call_count == 1


# ---------------------------------------------------------------------------
# 2. Input Guards & Validation
# ---------------------------------------------------------------------------

class TestGeminiScannerInputGuards:
    @pytest.mark.asyncio
    async def test_scan_rejects_oversized_image(self):
        """Images exceeding max_image_bytes (5MB) must be rejected before network call."""
        scanner = GeminiScanner(api_key="test-key-12345", max_image_bytes=100)
        oversized = b"x" * 101

        with pytest.raises(ValueError, match="exceeds limit"):
            await scanner.scan_image(oversized, "image/jpeg")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_mime", ["application/pdf", "image/webp", "image/gif", "text/plain"])
    async def test_scan_rejects_unsupported_mime_types(self, bad_mime: str):
        """Only image/jpeg and image/png are accepted."""
        scanner = GeminiScanner(api_key="test-key-12345")
        with pytest.raises(ValueError, match="Unsupported MIME type"):
            await scanner.scan_image(b"fake_data", bad_mime)


# ---------------------------------------------------------------------------
# 3. Structured Extraction & Normalization Tests
# ---------------------------------------------------------------------------

class TestGeminiScannerExtraction:
    @pytest.mark.asyncio
    async def test_successful_structured_extraction(self):
        """Valid JSON from Gemini is parsed and validated into a ScanResult dataclass."""
        scanner = GeminiScanner(api_key="test-key-12345")
        mock_response = _make_mock_response(json.dumps(SAMPLE_INVOICE_JSON))

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")

            assert isinstance(result, ScanResult)
            assert result.supplier_raw == "Lakshmi Supermarket"
            assert result.invoice_no == "INV-2026-999"
            assert result.invoice_date == "2026-11-25"
            assert result.currency == "INR"
            assert result.invoice_total == "1250.50"
            assert result.notes == "Clear printed bill"
            assert len(result.lines) == 2

            # Check line contents
            line1 = result.lines[0]
            assert line1["line_no"] == 1
            assert line1["product_raw"] == "Tata Salt 1kg"
            assert line1["quantity_raw"] == "10"
            assert line1["rate_raw"] == "28.00"
            assert line1["line_total_raw"] == "280.00"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fence_prefix,fence_suffix",
        [
            ("```json\n", "\n```"),
            ("```\n", "\n```"),
            ("```json\r\n", "\r\n```"),
        ],
    )
    async def test_strips_markdown_code_fences(self, fence_prefix: str, fence_suffix: str):
        """Markdown code fences returned by LLMs must be stripped cleanly."""
        scanner = GeminiScanner(api_key="test-key-12345")
        raw_json = json.dumps(SAMPLE_INVOICE_JSON)
        fenced_content = f"{fence_prefix}{raw_json}{fence_suffix}"
        mock_response = _make_mock_response(fenced_content)

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")
            assert result.invoice_no == "INV-2026-999"
            assert len(result.lines) == 2

    @pytest.mark.asyncio
    async def test_normalizes_non_iso_date_to_none(self):
        """Ambiguous or non-ISO dates must be set to None to avoid corrupting records."""
        scanner = GeminiScanner(api_key="test-key-12345")
        payload = dict(SAMPLE_INVOICE_JSON)
        payload["invoice_date"] = "25/11/2026"  # DD/MM/YYYY non-ISO
        mock_response = _make_mock_response(json.dumps(payload))

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")
            assert result.invoice_date is None

    @pytest.mark.asyncio
    async def test_handles_null_and_missing_optional_fields(self):
        """Null or empty optional fields in AI response normalize to None without error."""
        scanner = GeminiScanner(api_key="test-key-12345")
        payload = {
            "supplier_raw": None,
            "invoice_no": "",
            "invoice_date": None,
            "currency": None,
            "invoice_total": None,
            "notes": "   ",
            "lines": [
                {
                    "line_no": 1,
                    "product_raw": "Generic Item",
                    "pack_raw": None,
                    "quantity_raw": None,
                    "unit_raw": None,
                    "rate_raw": None,
                    "line_total_raw": None,
                }
            ],
        }
        mock_response = _make_mock_response(json.dumps(payload))

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")
            assert result.supplier_raw is None
            assert result.invoice_no is None
            assert result.notes is None
            assert len(result.lines) == 1
            assert result.lines[0]["product_raw"] == "Generic Item"
            assert result.lines[0]["quantity_raw"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("line_count", [0, 76])
    async def test_enforces_line_count_boundaries(self, line_count: int):
        """Line counts below 1 or above 75 must raise ValueError."""
        scanner = GeminiScanner(api_key="test-key-12345")
        lines = [
            {
                "line_no": i,
                "product_raw": f"Item {i}",
                "pack_raw": None,
                "quantity_raw": "1",
                "unit_raw": "PC",
                "rate_raw": "10",
                "line_total_raw": "10",
            }
            for i in range(1, line_count + 1)
        ]
        payload = dict(SAMPLE_INVOICE_JSON, lines=lines)
        mock_response = _make_mock_response(json.dumps(payload))

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            with pytest.raises(ValueError, match="must have between 1 and 75 entries"):
                await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")


# ---------------------------------------------------------------------------
# 4. Error Handling, Retries & API Key Masking
# ---------------------------------------------------------------------------

class TestGeminiScannerErrorHandling:
    @pytest.mark.asyncio
    async def test_permanent_error_fails_immediately_without_retry(self):
        """Permanent 401/403/400 errors must fail immediately without retrying."""
        scanner = GeminiScanner(api_key="AIzaSyDummySecretKey1234567890abcdef", max_retries=2)
        api_err = errors.APIError(401, {"error": {"message": "API key not valid: AIzaSyDummySecretKey1234567890abcdef"}})

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(side_effect=api_err)
            mock_get_client.return_value = mock_client

            with pytest.raises(RuntimeError) as exc_info:
                await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")

            # Must have attempted only once
            assert mock_client.aio.models.generate_content.call_count == 1
            err_msg = str(exc_info.value)
            assert "HTTP 401" in err_msg
            # Must redact the API key
            assert "AIzaSyDummySecretKey1234567890abcdef" not in err_msg
            assert "[REDACTED_API_KEY]" in err_msg

    @pytest.mark.asyncio
    async def test_transient_error_retries_and_succeeds(self):
        """Transient 429 error triggers retry and succeeds on subsequent attempt."""
        scanner = GeminiScanner(api_key="test-key-12345", max_retries=2)
        transient_err = errors.APIError(429, {"error": {"message": "Resource exhausted"}})
        mock_success = _make_mock_response(json.dumps(SAMPLE_INVOICE_JSON))

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(side_effect=[transient_err, mock_success])
            mock_get_client.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")
                assert result.invoice_no == "INV-2026-999"
                assert mock_client.aio.models.generate_content.call_count == 2
                mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_transient_error_exhausts_retries(self):
        """Transient 503 error exhausts max_retries and raises RuntimeError."""
        scanner = GeminiScanner(api_key="test-key-12345", max_retries=2)
        transient_err = errors.APIError(503, {"error": {"message": "Service unavailable"}})

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(side_effect=transient_err)
            mock_get_client.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(RuntimeError, match="after 3 attempts"):
                    await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")

            assert mock_client.aio.models.generate_content.call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_retries_and_raises(self):
        """TimeoutError triggers retries and raises TimeoutError when exhausted."""
        scanner = GeminiScanner(api_key="test-key-12345", request_timeout=1, max_retries=1)

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_get_client.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(TimeoutError, match="timed out"):
                    await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")

            assert mock_client.aio.models.generate_content.call_count == 2

    @pytest.mark.asyncio
    async def test_malformed_json_response_raises(self):
        """Non-JSON response text raises json.JSONDecodeError."""
        scanner = GeminiScanner(api_key="test-key-12345")
        mock_response = _make_mock_response("I could not read this bill image properly.")

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            with pytest.raises(json.JSONDecodeError):
                await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")

    @pytest.mark.asyncio
    async def test_empty_response_raises_value_error(self):
        """Empty response text raises ValueError."""
        scanner = GeminiScanner(api_key="test-key-12345")
        mock_response = _make_mock_response("")

        with patch.object(scanner, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            with pytest.raises(ValueError, match="empty response text"):
                await scanner.scan_image(b"fake_jpeg_bytes", "image/jpeg")

    def test_mask_api_key_helper(self):
        """mask_api_key redacts exact secret and any Google AIza... tokens."""
        secret = "my-custom-super-secret-key"
        msg = f"Failed with key {secret} and google token AIzaSyD9481abcdefghijklmnopqrstuv12345 in url"
        redacted = mask_api_key(msg, secret=secret)
        assert secret not in redacted
        assert "AIzaSyD9481abcdefghijklmnopqrstuv12345" not in redacted
        assert "[REDACTED_API_KEY]" in redacted


# ---------------------------------------------------------------------------
# 5. Preserved MockGeminiScanner Compatibility Tests
# ---------------------------------------------------------------------------

class TestMockGeminiScannerPreserved:
    @pytest.mark.asyncio
    async def test_mock_scanner_default_fixture(self):
        """MockGeminiScanner provides valid default fixture data."""
        scanner = MockGeminiScanner()
        result = await scanner.scan_image(b"fake_bytes", "image/jpeg")
        assert result.supplier_raw == "Ramesh Traders"
        assert result.invoice_no == "INV-001"
        assert len(result.lines) == 2

    @pytest.mark.asyncio
    async def test_mock_scanner_custom_fixture(self):
        """MockGeminiScanner allows overriding fixture response."""
        scanner = MockGeminiScanner(
            fixture_response={
                "supplier_raw": "Custom Supplier",
                "invoice_no": "INV-CUSTOM",
                "invoice_date": "2026-11-26",
                "currency": "INR",
                "invoice_total": "99.00",
                "notes": None,
                "lines": [
                    {
                        "line_no": 1,
                        "product_raw": "Item A",
                        "pack_raw": None,
                        "quantity_raw": "1",
                        "unit_raw": "PC",
                        "rate_raw": "99.00",
                        "line_total_raw": "99.00",
                    }
                ],
            }
        )
        result = await scanner.scan_image(b"fake_bytes", "image/jpeg")
        assert result.supplier_raw == "Custom Supplier"
        assert result.invoice_no == "INV-CUSTOM"
        assert len(result.lines) == 1
