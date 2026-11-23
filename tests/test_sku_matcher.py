"""
tests/test_sku_matcher.py
=========================
Unit tests for the KiranaIQ Auto-SKU fuzzy matcher.
"""

import pytest
from app.models import Product, ProductActive
from app.services.sku_matcher import SKUMatcher


@pytest.fixture
def sample_catalog():
    return [
        Product(sku="BISCUIT-PARLEG-10", product_name="Parle-G 10pk Biscuits", active=ProductActive.YES),
        Product(sku="ATTA-AASHIRVAAD-5KG", product_name="Aashirvaad Shudh Chakki Atta 5kg", active=ProductActive.YES),
        Product(sku="ATTA-AASHIRVAAD-10KG", product_name="Aashirvaad Shudh Chakki Atta 10kg", active=ProductActive.YES),
        Product(sku="OIL-FORTUNE-1L", product_name="Fortune Sunlite Refined Oil 1L", active=ProductActive.YES),
        Product(sku="BUTTER-AMUL-500G", product_name="Amul Pasteurised Butter 500g", active=ProductActive.YES),
        Product(sku="DISCONTINUED-PRODUCT", product_name="Old Discontinued Item", active=ProductActive.NO),
    ]


def test_exact_match(sample_catalog):
    matches = SKUMatcher.match("Parle-G 10pk Biscuits", "", sample_catalog)
    assert len(matches) > 0
    assert matches[0].sku == "BISCUIT-PARLEG-10"
    assert matches[0].confidence == 1.0
    assert matches[0].confidence_percent == 100


def test_fuzzy_match_with_typo(sample_catalog):
    # Missing hyphen, lowercase, 'biscuit' instead of 'biscuits'
    best = SKUMatcher.get_best_match("parle g 10pk biscuit", "", sample_catalog, auto_threshold=0.60)
    assert best is not None
    assert best.sku == "BISCUIT-PARLEG-10"
    assert best.confidence_percent >= 75


def test_pack_size_discrimination(sample_catalog):
    # Should choose 5kg atta, not 10kg atta
    best_5kg = SKUMatcher.get_best_match("Aashirvaad Atta", "5 kg", sample_catalog, auto_threshold=0.50)
    assert best_5kg is not None
    assert best_5kg.sku == "ATTA-AASHIRVAAD-5KG"

    # Should choose 10kg atta, not 5kg atta
    best_10kg = SKUMatcher.get_best_match("Aashirvaad Atta", "10 kg", sample_catalog, auto_threshold=0.50)
    assert best_10kg is not None
    assert best_10kg.sku == "ATTA-AASHIRVAAD-10KG"


def test_inactive_products_excluded(sample_catalog):
    matches = SKUMatcher.match("Old Discontinued Item", "", sample_catalog)
    assert all(m.sku != "DISCONTINUED-PRODUCT" for m in matches)
