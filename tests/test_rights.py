"""Tests for rights normalization and policy evaluation."""
import pytest
from proproom.core.rights import evaluate_rights, normalize_license


def test_normalize_license():
    assert normalize_license("CC BY-SA 4.0") == "cc-by-sa-4.0"
    assert normalize_license("CC0 1.0 Universal") == "cc0-1.0"
    assert normalize_license("Public Domain") == "public-domain"
    assert normalize_license("CC-BY 3.0") == "cc-by-3.0"
    assert normalize_license("Pexels License") == "pexels-license"
    assert normalize_license("<a href='...'>CC BY 2.0</a>") == "cc-by-2.0"


def test_evaluate_rights_defaults():
    policy = {
        "rights": {
            "allow": ["cc0", "pd", "cc-by", "cc-by-sa"],
            "deny": ["nc", "nd", "fair-use", "none", "editorial"],
            "deny_publishers": ["AP", "Reuters", "Getty", "AFP"],
            "synthetic_excluded_anchors": ["person", "event", "data"],
        }
    }

    # CC BY-SA 4.0 should be allowed with attribution and share_alike
    dec, reasons, obs = evaluate_rights(policy, basis="license", license_id="cc-by-sa-4.0")
    assert dec == "allow"
    assert "attribution" in obs
    assert "share_alike" in obs

    # CC BY-NC 4.0 should be denied
    dec, reasons, obs = evaluate_rights(policy, basis="license", license_id="cc-by-nc-4.0")
    assert dec == "deny"

    # Public domain with valid reason
    dec, reasons, obs = evaluate_rights(policy, basis="public_domain", pd_reason="pd-old")
    assert dec == "allow"

    # Publisher denied
    dec, reasons, obs = evaluate_rights(policy, basis="license", license_id="cc-by-4.0", publisher="Associated Press (AP)")
    assert dec == "deny"

    # Synthetic with person anchor denied
    dec, reasons, obs = evaluate_rights(policy, basis="own_work", synthetic=True, anchor="person")
    assert dec == "deny"

    # Quotation requires review
    dec, reasons, obs = evaluate_rights(policy, basis="quotation")
    assert dec == "review"
