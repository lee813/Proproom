"""§6 rights and attribution."""
from __future__ import annotations

import pytest
from support import gap, ingest, policy_rules

from proproom.core.rights import evaluate_rights, normalize_license


@pytest.mark.parametrize("lic", ["cc0-1.0", "cc-by-4.0", "cc-by-2.0", "cc-by-sa-4.0", "cc-by-sa-3.0"])
def test_default_policy_allows_open_licences(conn, lic):
    assert evaluate_rights(policy_rules(conn), basis="license", license_id=lic)[0] == "allow"


@pytest.mark.parametrize("lic", ["cc-by-nc-4.0", "cc-by-nd-4.0", "cc-by-nc-sa-2.0", "cc-by-nc-nd-3.0"])
def test_default_policy_denies_nc_nd(conn, lic):
    assert evaluate_rights(policy_rules(conn), basis="license", license_id=lic)[0] == "deny"


@pytest.mark.parametrize("pub", ["Associated Press (AP)", "Reuters", "Getty Images", "AFP"])
def test_wire_and_agency_images_rejected(conn, pub):
    assert evaluate_rights(policy_rules(conn), basis="license", license_id="cc-by-4.0", publisher=pub)[0] == "deny"


def test_material_is_usable_unless_evidence_says_otherwise(conn):
    """§6: missing, unrecognised or unexplained rights info is allowed and noted; only evidence blocks."""
    rules = policy_rules(conn)
    for kw in ({"basis": "unknown"}, {"basis": "public_domain"},
               {"basis": "license", "license_id": "open-government-licence-v3.0"},
               {"basis": "license", "license_id": "standard-license"},
               {"basis": "provider_license", "license_id": "some-stock-license"},
               {"basis": "license", "license_id": "cc-by-sa-4.0", "restrictions": ["personality"]}):
        dec, reasons, _ = evaluate_rights(rules, **kw)
        assert dec == "allow" and reasons, kw


def test_disputed_files_are_denied(conn):
    """§6: a deletion request or copyvio tag at the source is evidence against use."""
    assert evaluate_rights(policy_rules(conn), basis="license", license_id="cc-by-4.0",
                           restrictions=["disputed"])[0] == "deny"


def test_obligations_attribution_and_share_alike(conn):
    """§6: CC BY-SA carries attribution + share-alike obligations for the delivery plan."""
    _, _, obs = evaluate_rights(policy_rules(conn), basis="license", license_id="cc-by-sa-4.0")
    assert {"attribution", "share_alike"} <= set(obs)
    _, _, obs = evaluate_rights(policy_rules(conn), basis="license", license_id="cc0-1.0")
    assert "share_alike" not in obs


def test_plugins_can_only_restrict(conn):
    """§6 / §11.2: a plugin 'allow' never upgrades a core deny; a plugin 'deny' always wins."""
    rules = policy_rules(conn)
    assert evaluate_rights(rules, basis="license", license_id="cc-by-nc-4.0",
                           plugin_restrictions=[{"decision": "allow", "plugin": "p"}])[0] == "deny"
    assert evaluate_rights(rules, basis="license", license_id="cc-by-4.0",
                           plugin_restrictions=[{"decision": "deny", "code": "no_map", "plugin": "p"}])[0] == "deny"


def test_synthetic_never_for_person_event_data(conn):
    """§6: synthetic media is illustration only — denied for person/event/data anchors, disclosure obligation."""
    rules = policy_rules(conn)
    for anchor in ("person", "event", "data"):
        assert evaluate_rights(rules, basis="own_work", synthetic=True, anchor=anchor)[0] == "deny"
    dec, _, obs = evaluate_rights(rules, basis="own_work", synthetic=True, anchor="work")
    assert dec == "allow" and "synthetic_disclosure" in obs


def test_quotation_needs_explicit_confirmation(conn):
    """§6: quotation is modelled separately and is never silently allowed."""
    assert evaluate_rights(policy_rules(conn), basis="quotation")[0] == "review"


def test_public_domain_without_a_reason_is_noted(conn):
    """§6: public_domain should state the concrete reason; a bare 'PD' is still usable, with the gap noted."""
    for kw in ({"basis": "public_domain"},
               {"basis": "license", "license_id": normalize_license("Public domain")}):
        dec, reasons, _ = evaluate_rights(policy_rules(conn), **kw)
        assert dec == "allow" and reasons[-1]["code"] == "public_domain_unverified"


def test_assessment_keeps_basis_for_review(conn, home):
    """§6 / B.1: the assessment records the basis the decision rests on, whatever the decision."""
    q = ingest(conn, home, slug="press-release", title="Press release", kind="screenshot", basis="quotation",
               license_id=None)
    assert conn.execute("SELECT basis FROM rights_assessment WHERE id = ?",
                        (q.assessment_id,)).fetchone()[0] == "quotation"


def test_restrictions_raise_a_flag(conn, home):
    """§6: Commons Restrictions / FoP must be recorded and tagged with a `restriction` flag."""
    conn.execute("BEGIN IMMEDIATE")
    from proproom.core import dedupe
    aid, _ = dedupe.register_asset(conn, slug="logo", kind="photo", title="Logo")
    prov = dedupe.register_provenance(conn, asset_id=aid, source="commons", external_id="File:Logo.png")
    dedupe.register_rights(conn, provenance_id=prov, asset_id=aid, policy_version=1, policy_rules=policy_rules(conn),
                           basis="license", license_id="cc-by-sa-4.0", restrictions=["trademarked"])
    conn.execute("COMMIT")
    assert conn.execute("SELECT count(*) FROM flag WHERE target_id = ? AND kind = 'restriction'",
                        (aid,)).fetchone()[0] == 1

