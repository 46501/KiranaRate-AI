"""
app/services/sku_matcher.py
===========================
Intelligent Fuzzy SKU Matching Engine for KiranaIQ.

Matches raw extracted invoice line text and pack descriptions with canonical
active products in the store's catalogue. Uses standard library difflib and
token-based similarity with domain-aware pack size normalization so zero
external heavy dependencies are required.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.models import Product, ProductActive


@dataclass
class MatchResult:
    """Result of an SKU match attempt."""
    sku: str
    product_name: str
    confidence: float  # 0.0 to 1.0
    confidence_percent: int  # 0 to 100
    matched_pack: bool = False


class SKUMatcher:
    """Fuzzy matching engine for Kirana products."""

    _UNIT_SYNONYMS = {
        r"\b(pk|pck|pkt|packet|packets)\b": "pack",
        r"\b(gm|gms|grams|gram)\b": "g",
        r"\b(kilos|kilo|kilogram|kilograms)\b": "kg",
        r"\b(ltr|litre|litres|liters|liter)\b": "l",
        r"\b(milli|millilitre|milliliter|mls)\b": "ml",
        r"\b(pcs|pc|pieces|piece)\b": "pack",
    }

    @classmethod
    def normalize_text(cls, text: str) -> str:
        """Clean and normalize product text for comparison."""
        if not text:
            return ""
        norm = text.lower().strip()
        # Replace punctuation with spaces
        norm = re.sub(r"[-_/.,|()]", " ", norm)
        # Apply unit synonyms
        for pattern, replacement in cls._UNIT_SYNONYMS.items():
            norm = re.sub(pattern, replacement, norm)
        # Close gap between numbers and units: e.g. "5 kg" -> "5kg", "500 g" -> "500g"
        norm = re.sub(r"\b(\d+(?:[.]\d+)?)\s*(g|kg|l|ml|pack)\b", r"\1\2", norm)
        # Collapse multiple spaces
        norm = re.sub(r"\s+", " ", norm).strip()
        return norm

    @classmethod
    def extract_pack_tokens(cls, text: str) -> set[str]:
        """Extract size/weight tokens like '10', '500g', '5kg', '1l'."""
        if not text:
            return set()
        clean = cls.normalize_text(text)
        tokens = set(re.findall(r"\b\d+(?:[.]\d+)?(?:g|kg|l|ml|pack)?\b", clean))
        return tokens

    @classmethod
    def match(
        cls,
        product_raw: str,
        pack_raw: Optional[str] = None,
        products: Optional[List[Product]] = None,
        min_confidence: float = 0.35,
    ) -> List[MatchResult]:
        """
        Find best matching SKUs for a raw line item description and pack.
        """
        if not products or not product_raw:
            return []

        active_products = [
            p for p in products
            if getattr(p.active, "value", p.active) == "YES"
        ]
        if not active_products:
            return []

        combined_raw = f"{product_raw} {pack_raw or ''}".strip()
        norm_raw = cls.normalize_text(combined_raw)
        raw_tokens = set(norm_raw.split())
        raw_pack_tokens = cls.extract_pack_tokens(combined_raw)

        results: List[MatchResult] = []

        for prod in active_products:
            norm_name = cls.normalize_text(prod.product_name)
            norm_sku = cls.normalize_text(prod.sku)
            name_tokens = set(norm_name.split())
            sku_tokens = set(norm_sku.split())
            target_tokens = name_tokens | sku_tokens
            target_pack_tokens = cls.extract_pack_tokens(f"{prod.product_name} {prod.sku}")

            # 1. Exact match shortcut
            if norm_raw == norm_name or norm_raw == norm_sku:
                results.append(
                    MatchResult(
                        sku=prod.sku,
                        product_name=prod.product_name,
                        confidence=1.0,
                        confidence_percent=100,
                        matched_pack=True,
                    )
                )
                continue

            # 2. Token overlap: precision (how much of raw input is explained) & recall
            # Allow prefix/plural matching for tokens (e.g. biscuit ~ biscuits)
            matched_raw_count = 0
            for r_tok in raw_tokens:
                if any(r_tok == t_tok or (len(r_tok) > 3 and (r_tok.startswith(t_tok) or t_tok.startswith(r_tok))) for t_tok in target_tokens):
                    matched_raw_count += 1

            token_precision = matched_raw_count / max(1, len(raw_tokens))
            overlap_name = len(raw_tokens & name_tokens) / max(1, len(name_tokens))
            token_score = (token_precision * 0.65) + (overlap_name * 0.35)

            # 3. String sequence similarity score
            seq_score_name = difflib.SequenceMatcher(None, norm_raw, norm_name).ratio()
            seq_score_sku = difflib.SequenceMatcher(None, norm_raw, norm_sku).ratio()
            seq_score = max(seq_score_name, seq_score_sku)

            # 4. Pack token match bonus / penalty
            pack_bonus = 0.0
            matched_pack = False
            if raw_pack_tokens and target_pack_tokens:
                if raw_pack_tokens & target_pack_tokens:
                    pack_bonus = 0.20
                    matched_pack = True
                else:
                    pack_bonus = -0.30

            # 5. Composite score
            base_score = (token_score * 0.50) + (seq_score * 0.35) + pack_bonus
            final_score = max(0.0, min(0.99, base_score))

            if final_score >= min_confidence:
                results.append(
                    MatchResult(
                        sku=prod.sku,
                        product_name=prod.product_name,
                        confidence=round(final_score, 2),
                        confidence_percent=int(round(final_score * 100)),
                        matched_pack=matched_pack,
                    )
                )

        results.sort(key=lambda x: x.confidence, reverse=True)
        return results

    @classmethod
    def get_best_match(
        cls,
        product_raw: str,
        pack_raw: Optional[str] = None,
        products: Optional[List[Product]] = None,
        auto_threshold: float = 0.50,
    ) -> Optional[MatchResult]:
        """Return the top matching SKU if confidence exceeds auto_threshold, else None."""
        matches = cls.match(product_raw, pack_raw, products, min_confidence=auto_threshold)
        return matches[0] if matches else None
