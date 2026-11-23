"""
app/services/tax_engine.py
==========================
Indian GST & Input Tax Credit (ITC) calculation engine for KiranaIQ.

Calculates estimated Goods & Services Tax (GST) breakdown, CGST/SGST split,
and claimable Input Tax Credit (ITC) for GSTR-3B & GSTR-2B reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from app.models import Bill, BillStatus, Purchase
from app.storage.base import BaseStore


# Standard Indian FMCG GST Slabs by SKU keyword / category
_SLAB_KEYWORDS = {
    0.05: ["oil", "atta", "flour", "sugar", "tea", "dal", "pulse", "rice", "spice", "masala"],
    0.12: ["butter", "ghee", "cheese", "juice", "ketchup", "sauce"],
    0.18: ["biscuit", "soap", "detergent", "shampoo", "paste", "chocolate", "cleaner", "snack"],
    0.28: ["drink", "soda", "cold drink", "tobacco"],
}


@dataclass
class SupplierTaxSummary:
    supplier_id: str
    supplier_name: str
    bills_count: int
    purchases_count: int
    taxable_amount_inr: float
    cgst_inr: float
    sgst_inr: float
    total_itc_inr: float
    gross_amount_inr: float


@dataclass
class GSTReport:
    total_taxable_inr: float
    total_cgst_inr: float
    total_sgst_inr: float
    total_itc_inr: float
    total_gross_inr: float
    by_supplier: List[SupplierTaxSummary]
    by_slab: Dict[str, float]


class TaxEngine:
    """Computes Indian retail GST & ITC reconciliation metrics."""

    @classmethod
    def get_estimated_gst_rate(cls, sku: str, product_name: str) -> float:
        """Estimate GST rate (e.g. 0.05, 0.12, 0.18) based on SKU and product name."""
        combined = f"{sku} {product_name}".lower()
        for rate, keywords in _SLAB_KEYWORDS.items():
            if any(kw in combined for kw in keywords):
                return rate
        # Default FMCG packaged goods standard slab is 5%
        return 0.05

    @classmethod
    def compute_gst_report(cls, store: BaseStore) -> GSTReport:
        """
        Compute full GST & ITC summary across all recorded purchases.
        """
        suppliers = {s.supplier_id: s.supplier_name for s in store.list_suppliers()}
        products = {p.sku: p.product_name for p in store.list_products(active_only=False)}
        purchases = store.list_purchases(limit=1000)

        # Supplier buckets
        supplier_data: Dict[str, dict] = {}
        slab_data: Dict[str, float] = {"0%": 0.0, "5%": 0.0, "12%": 0.0, "18%": 0.0, "28%": 0.0}

        total_taxable = 0.0
        total_itc = 0.0

        for p in purchases:
            taxable_inr = round(p.net_paise / 100, 2)
            prod_name = products.get(p.sku, p.sku)
            rate = cls.get_estimated_gst_rate(p.sku, prod_name)

            tax_inr = round(taxable_inr * rate, 2)
            cgst = round(tax_inr / 2, 2)
            sgst = round(tax_inr - cgst, 2)
            itc = cgst + sgst

            total_taxable += taxable_inr
            total_itc += itc

            slab_key = f"{int(rate * 100)}%"
            slab_data[slab_key] = round(slab_data.get(slab_key, 0.0) + itc, 2)

            sup_id = p.supplier_id
            if sup_id not in supplier_data:
                supplier_data[sup_id] = {
                    "supplier_id": sup_id,
                    "supplier_name": suppliers.get(sup_id, sup_id),
                    "bills": set(),
                    "purchases_count": 0,
                    "taxable": 0.0,
                    "cgst": 0.0,
                    "sgst": 0.0,
                    "itc": 0.0,
                }

            s_entry = supplier_data[sup_id]
            s_entry["bills"].add(p.bill_id)
            s_entry["purchases_count"] += 1
            s_entry["taxable"] += taxable_inr
            s_entry["cgst"] += cgst
            s_entry["sgst"] += sgst
            s_entry["itc"] += itc

        total_cgst = round(total_itc / 2, 2)
        total_sgst = round(total_itc - total_cgst, 2)
        total_gross = round(total_taxable + total_itc, 2)

        summaries: List[SupplierTaxSummary] = []
        for s in supplier_data.values():
            taxable = round(s["taxable"], 2)
            itc = round(s["itc"], 2)
            cgst = round(s["cgst"], 2)
            sgst = round(s["sgst"], 2)
            summaries.append(
                SupplierTaxSummary(
                    supplier_id=s["supplier_id"],
                    supplier_name=s["supplier_name"],
                    bills_count=len(s["bills"]),
                    purchases_count=s["purchases_count"],
                    taxable_amount_inr=taxable,
                    cgst_inr=cgst,
                    sgst_inr=sgst,
                    total_itc_inr=itc,
                    gross_amount_inr=round(taxable + itc, 2),
                )
            )

        summaries.sort(key=lambda x: x.taxable_amount_inr, reverse=True)

        return GSTReport(
            total_taxable_inr=round(total_taxable, 2),
            total_cgst_inr=total_cgst,
            total_sgst_inr=total_sgst,
            total_itc_inr=round(total_itc, 2),
            total_gross_inr=total_gross,
            by_supplier=summaries,
            by_slab=slab_data,
        )
