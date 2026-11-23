"""
app/services/negotiation_generator.py
=====================================
Automated WhatsApp Supplier Negotiation Note Generator for KiranaIQ.

Generates pre-formatted English and Hinglish commercial rate negotiation notes
with 1-click WhatsApp deep-links so store owners can instantly negotiate lower
wholesale rates with distributors.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Optional

from app.models import Alert
from app.storage.base import BaseStore


@dataclass
class NegotiationNote:
    alert_id: str
    sku: str
    product_name: str
    high_supplier: str
    low_supplier: str
    high_rate_inr: float
    low_rate_inr: float
    gap_diff_inr: float
    gap_pct: float
    high_date: str
    english_message: str
    hinglish_message: str
    whatsapp_url_en: str
    whatsapp_url_hi: str


class NegotiationGenerator:
    """Generates negotiation notes from price-gap alerts."""

    @classmethod
    def generate_note(cls, alert: Alert, store: BaseStore) -> NegotiationNote:
        product = store.get_product(alert.sku)
        product_name = product.product_name if product else alert.sku

        high_rate = round(alert.high_rate / 100, 2)
        low_rate = round(alert.low_rate / 100, 2)
        gap_diff = round(high_rate - low_rate, 2)
        gap_pct = round(alert.gap_pct, 2)
        high_date = alert.high_date.strftime("%d-%b-%Y")

        en_msg = (
            f"Namaste {alert.high_supplier},\n\n"
            f"Regarding our purchase of {product_name} ({alert.sku}) on {high_date}:\n"
            f"Our invoice rate was Rs {high_rate:.2f}/unit.\n"
            f"We have verified competitive wholesale rates for the identical SKU at "
            f"Rs {low_rate:.2f}/unit ({gap_pct:.2f}% lower, Rs {gap_diff:.2f}/unit savings).\n\n"
            f"Kindly review and match the rate of Rs {low_rate:.2f}/unit for our upcoming purchase order.\n\n"
            f"Regards,\n"
            f"Store Purchase Department\n"
            f"(Generated via KiranaIQ Purchase Intelligence)"
        )

        hi_msg = (
            f"Namaste {alert.high_supplier} ji,\n\n"
            f"Hamare {high_date} ke bill me {product_name} ({alert.sku}) ka rate Rs {high_rate:.2f}/unit laga hai.\n"
            f"Hame doosre verified supplier se yahi item Rs {low_rate:.2f}/unit me supply mila hai "
            f"({gap_pct:.2f}% kam rate, Rs {gap_diff:.2f}/unit ka farq).\n\n"
            f"Aap se nivedan hai ki hamare agle order ke liye Rs {low_rate:.2f}/unit ka rate confirm karein.\n\n"
            f"Dhanyawad,\n"
            f"Store Purchase Manager\n"
            f"(KiranaIQ Purchase Intelligence)"
        )

        url_en = f"https://api.whatsapp.com/send?text={urllib.parse.quote(en_msg)}"
        url_hi = f"https://api.whatsapp.com/send?text={urllib.parse.quote(hi_msg)}"

        return NegotiationNote(
            alert_id=alert.alert_id,
            sku=alert.sku,
            product_name=product_name,
            high_supplier=alert.high_supplier,
            low_supplier=alert.low_supplier,
            high_rate_inr=high_rate,
            low_rate_inr=low_rate,
            gap_diff_inr=gap_diff,
            gap_pct=gap_pct,
            high_date=high_date,
            english_message=en_msg,
            hinglish_message=hi_msg,
            whatsapp_url_en=url_en,
            whatsapp_url_hi=url_hi,
        )
