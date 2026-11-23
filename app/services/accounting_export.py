"""
app/services/accounting_export.py
=================================
Accountant export utilities for KiranaIQ:
1. CSV export for Microsoft Excel / Google Sheets
2. Tally Prime XML purchase voucher export
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from app.models import Bill, BillStatus, Purchase
from app.storage.base import BaseStore


class AccountingExporter:
    """Generates accounting exports for Kirana retail stores."""

    @classmethod
    def generate_csv(cls, store: BaseStore) -> str:
        """
        Generate CSV string of all recorded purchases with invoice and supplier details.
        """
        output = io.StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow([
            "Invoice Date",
            "Invoice No",
            "Supplier ID",
            "Supplier Name",
            "Bill ID",
            "SKU",
            "Product Name",
            "Units",
            "Net Rate (INR)",
            "Net Amount (INR)",
            "Cost Basis",
            "Posted At",
        ])

        suppliers = {s.supplier_id: s.supplier_name for s in store.list_suppliers()}
        products = {p.sku: p.product_name for p in store.list_products(active_only=False)}
        purchases = store.list_purchases(limit=1000)

        for p in purchases:
            rate_inr = f"{round(p.net_paise / (p.units * 100), 2):.2f}" if p.units > 0 else "0.00"
            amt_inr = f"{round(p.net_paise / 100, 2):.2f}"
            writer.writerow([
                p.invoice_date.isoformat(),
                p.invoice_no,
                p.supplier_id,
                suppliers.get(p.supplier_id, p.supplier_id),
                p.bill_id,
                p.sku,
                products.get(p.sku, p.sku),
                p.units,
                rate_inr,
                amt_inr,
                getattr(p.cost_basis, "value", str(p.cost_basis)),
                p.posted_at.strftime("%Y-%m-%d %H:%M:%S") if p.posted_at else "",
            ])

        return output.getvalue()

    @classmethod
    def generate_tally_xml(cls, store: BaseStore) -> str:
        """
        Generate Tally Prime compatible XML purchase vouchers for posted bills.
        Format compliant with Tally XML Import Specification.
        """
        suppliers = {s.supplier_id: s.supplier_name for s in store.list_suppliers()}
        products = {p.sku: p for p in store.list_products(active_only=False)}

        # Get all POSTED bills
        posted_bills = [b for b in store.list_bills() if b.status == BillStatus.POSTED]

        # Root envelope
        envelope = ET.Element("ENVELOPE")
        header = ET.SubElement(envelope, "HEADER")
        tally_req = ET.SubElement(header, "TALLYREQUEST")
        tally_req.text = "Import Data"

        body = ET.SubElement(envelope, "BODY")
        import_data = ET.SubElement(body, "IMPORTDATA")
        req_data = ET.SubElement(import_data, "REQUESTDATA")

        for bill in posted_bills:
            purchases = store.get_purchases_by_bill(bill.bill_id)
            if not purchases:
                continue

            supplier_name = suppliers.get(bill.supplier_id or "", bill.supplier_raw or "Cash Purchase")
            inv_date_str = bill.invoice_date.replace("-", "") if bill.invoice_date else datetime.now(timezone.utc).strftime("%Y%m%d")
            total_paise = sum(p.net_paise for p in purchases)
            total_inr = round(total_paise / 100, 2)

            tally_msg = ET.SubElement(req_data, "TALLYMESSAGE")
            voucher = ET.SubElement(
                tally_msg,
                "VOUCHER",
                VCHTYPE="Purchase",
                ACTION="Create",
                OBJVIEW="Invoice Voucher View",
            )

            # Header voucher fields
            v_date = ET.SubElement(voucher, "DATE")
            v_date.text = inv_date_str

            v_type = ET.SubElement(voucher, "VOUCHERTYPENAME")
            v_type.text = "Purchase"

            v_no = ET.SubElement(voucher, "VOUCHERNUMBER")
            v_no.text = bill.invoice_no or bill.bill_id

            v_ref = ET.SubElement(voucher, "REFERENCE")
            v_ref.text = bill.bill_id

            party = ET.SubElement(voucher, "PARTYLEDGERNAME")
            party.text = supplier_name

            narr = ET.SubElement(voucher, "NARRATION")
            narr.text = f"Imported from KiranaIQ. Bill ID: {bill.bill_id} | Reviewer: {bill.reviewer or 'system'}"

            # Inventory line items
            for p in purchases:
                prod = products.get(p.sku)
                item_name = prod.product_name if prod else p.sku
                unit_name = prod.base_unit if prod else "PACK"
                rate_inr = round(p.net_paise / (p.units * 100), 2) if p.units > 0 else 0.0
                line_inr = round(p.net_paise / 100, 2)

                inv_entry = ET.SubElement(voucher, "ALLINVENTORYENTRIES.LIST")
                i_name = ET.SubElement(inv_entry, "STOCKITEMNAME")
                i_name.text = item_name

                i_act_qty = ET.SubElement(inv_entry, "ACTUALQTY")
                i_act_qty.text = f"{p.units} {unit_name}"

                i_bil_qty = ET.SubElement(inv_entry, "BILLEDQTY")
                i_bil_qty.text = f"{p.units} {unit_name}"

                i_rate = ET.SubElement(inv_entry, "RATE")
                i_rate.text = f"{rate_inr:.2f}/{unit_name}"

                i_amount = ET.SubElement(inv_entry, "AMOUNT")
                i_amount.text = f"-{line_inr:.2f}"

            # Supplier Credit Ledger Entry
            sup_ledger = ET.SubElement(voucher, "LEDGERENTRIES.LIST")
            sup_name = ET.SubElement(sup_ledger, "LEDGERNAME")
            sup_name.text = supplier_name
            sup_pos = ET.SubElement(sup_ledger, "ISDEEMEDPOSITIVE")
            sup_pos.text = "No"
            sup_amt = ET.SubElement(sup_ledger, "AMOUNT")
            sup_amt.text = f"{total_inr:.2f}"

            # Purchase Account Debit Ledger Entry
            pur_ledger = ET.SubElement(voucher, "LEDGERENTRIES.LIST")
            pur_name = ET.SubElement(pur_ledger, "LEDGERNAME")
            pur_name.text = "Purchase Account"
            pur_pos = ET.SubElement(pur_ledger, "ISDEEMEDPOSITIVE")
            pur_pos.text = "Yes"
            pur_amt = ET.SubElement(pur_ledger, "AMOUNT")
            pur_amt.text = f"-{total_inr:.2f}"

        xml_str = ET.tostring(envelope, encoding="utf-8", xml_declaration=True).decode("utf-8")
        return xml_str
