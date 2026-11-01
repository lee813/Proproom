"""Rights normalisation (basis/licence/pd_reason), policy evaluation, rights_assessment writes.

Plugins may only restrict. Handover §6.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from .ids import ulid, utcnow, utcnow_monotonic

Decision = Literal["allow", "review", "deny"]
Basis = Literal["license", "public_domain", "provider_license", "quotation", "own_work", "unknown"]

_HTML_TAGS = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return _HTML_TAGS.sub("", text).strip()


def normalize_license(raw: str | None) -> str | None:
    """Normalize common license strings to canonical identifiers, e.g. cc-by-sa-4.0, cc0-1.0."""
    if not raw:
        return None
    s = strip_html(raw).lower().replace("_", "-").strip()
    s = re.sub(r"\s+", " ", s)

    if "cc0" in s or "zero" in s:
        return "cc0-1.0"
    if "public domain" in s or s in ("pd", "pdm", "pd-old", "pd-us-gov"):
        return "public-domain"
    if "pexels" in s:
        return "pexels-license"
    if "pixabay" in s:
        return "pixabay-content-license"

    # Creative Commons variants
    # Match patterns like cc by-sa 4.0, cc-by 3.0, cc by-nc-sa 2.0
    cc_match = re.search(r"cc[\s\-]+(by(?:-?[a-z]{2})*)(?:[\s\-]+([0-9\.]+))?", s)
    if cc_match:
        clauses = cc_match.group(1).replace(" ", "-")
        ver = cc_match.group(2) or "4.0"
        return f"cc-{clauses}-{ver}"

    return s.replace(" ", "-")


def evaluate_rights(
    policy_rules: dict[str, Any],
    basis: str,
    license_id: str | None = None,
    pd_reason: str | None = None,
    restrictions: list[str] | None = None,
    publisher: str | None = None,
    synthetic: bool = False,
    anchor: str | None = None,
    plugin_restrictions: list[dict[str, Any]] | None = None,
) -> tuple[Decision, list[dict[str, Any]], list[str]]:
    """Evaluate rights evidence against policy rules.

    Returns:
      decision: 'allow' | 'review' | 'deny'
      reasons: list of {code, detail, plugin?}
      obligations: list of obligation codes (e.g. 'attribution', 'share_alike', 'synthetic_disclosure')
    """
    rights_cfg = policy_rules.get("rights", policy_rules)
    allow_families = [x.lower() for x in rights_cfg.get("allow", ["cc0", "pd", "cc-by", "cc-by-sa"])]
    deny_families = [x.lower() for x in rights_cfg.get("deny", ["nc", "nd", "fair-use", "none", "editorial"])]
    deny_publishers = [p.lower() for p in rights_cfg.get("deny_publishers", ["ap", "reuters", "getty", "afp"])]
    synth_excluded = [a.lower() for a in rights_cfg.get("synthetic_excluded_anchors", ["person", "event", "data"])]

    reasons: list[dict[str, Any]] = []
    obligations: list[str] = []
    decision: Decision = "allow"

    # Publisher check
    if publisher and any(re.search(r"(?<!\w)" + re.escape(dp) + r"(?!\w)", publisher.lower()) for dp in deny_publishers if dp):
        reasons.append({"code": "publisher_denied", "detail": f"Publisher '{publisher}' is restricted by policy."})
        return "deny", reasons, obligations

    # Restrictions (trademark, personality, fop, etc.)
    if restrictions:
        for r in restrictions:
            if r in ("trademarked", "personality", "fop", "restriction"):
                reasons.append({"code": "restriction_present", "detail": f"Material has restriction: {r}"})
                if decision != "deny":
                    decision = "review"

    # Synthetic content handling
    if synthetic:
        obligations.append("synthetic_disclosure")
        if anchor and anchor.lower() in synth_excluded:
            reasons.append({"code": "synthetic_anchor_disallowed",
                            "detail": f"Synthetic media disallowed for anchor '{anchor}'."})
            return "deny", reasons, obligations

    # Basis handling
    norm_lic = (license_id or "").lower()

    if basis == "unknown":
        reasons.append({"code": "unknown_rights", "detail": "Rights status is unknown or incomplete."})
        return "deny", reasons, obligations

    elif basis == "own_work":
        reasons.append({"code": "own_work", "detail": "Own original creation."})

    elif basis == "public_domain":
        valid_pd = {"pd-old", "pd-us-gov", "pd-mark", "cc0", "cc0-1.0"}
        if pd_reason and pd_reason.lower() in valid_pd:
            reasons.append({"code": "public_domain", "detail": f"Public domain basis ({pd_reason})."})
        else:
            reasons.append({"code": "public_domain_unverified",
                            "detail": f"Public domain claim needs review ({pd_reason})."})
            if decision != "deny":
                decision = "review"

    elif basis == "quotation":
        reasons.append({"code": "quotation", "detail": "Quotation / screenshot requiring explicit confirmation."})
        obligations.append("quotation_confirmation")
        if decision != "deny":
            decision = "review"

    elif basis in ("license", "provider_license"):
        # Check deny families first
        for df in deny_families:
            if df in norm_lic:
                reasons.append({"code": f"license_{df}", "detail": f"License matches denied family: {df}."})
                return "deny", reasons, obligations

        # Check obligations
        if "by" in norm_lic:
            obligations.append("attribution")
        if "sa" in norm_lic:
            obligations.append("share_alike")

        # Check allow families
        matched_allow = False
        if norm_lic == "public-domain":
            valid_pd = {"pd-old", "pd-us-gov", "pd-mark", "cc0", "cc0-1.0"}
            if pd_reason and pd_reason.lower() in valid_pd:
                matched_allow = True
            else:
                reasons.append({"code": "public_domain_unverified", "detail": "Public domain license requires specific pd_reason."})
                if decision != "deny":
                    decision = "review"
        else:
            for af in allow_families:
                if af == "cc-by" and re.search(r"cc-by(?!-nc|-nd|-sa)", norm_lic):
                    matched_allow = True
                    break
                if af == "cc-by-sa" and "cc-by-sa" in norm_lic:
                    matched_allow = True
                    break
                if af == "cc0" and "cc0" in norm_lic:
                    matched_allow = True
                    break
                if af == norm_lic:
                    matched_allow = True
                    break

        if matched_allow:
            reasons.append({"code": "license_allowed", "detail": f"License allowed: {norm_lic}"})
        elif basis == "provider_license":
            reasons.append({"code": "provider_license", "detail": f"Provider license '{norm_lic}' requires review."})
            if decision != "deny":
                decision = "review"
        else:
            reasons.append({"code": "license_unrecognized", "detail": f"License '{norm_lic}' is not in allowlist."})
            return "deny", reasons, obligations

    # Apply plugin restrictions (plugins may only restrict!)
    if plugin_restrictions:
        for pr in plugin_restrictions:
            p_dec = pr.get("decision", "allow")
            if p_dec == "deny":
                decision = "deny"
                reasons.append({"code": pr.get("code", "plugin_deny"), "detail": pr.get("detail", ""),
                                "plugin": pr.get("plugin")})
            elif p_dec == "review" and decision != "deny":
                decision = "review"
                reasons.append({"code": pr.get("code", "plugin_review"), "detail": pr.get("detail", ""),
                                "plugin": pr.get("plugin")})

    return decision, reasons, obligations
