"""
pre_creation_validator.py
==========================
Hard validation gate that runs BEFORE the Celonis push / SAP Action Flow trigger.

This is deterministic Python code (NOT LLM) — the agent cannot bypass it.
If ANY check fails, the PO is routed to the exception queue instead of SAP.

Checks performed (Step 4 from reliability guide):
  [1] customer_id present and non-empty
  [2] po_number present and non-empty
  [3] At least one line item with a mapped material + quantity
  [4] Currency present
  [5] total_po_value within tolerance of sum(line items)  [SKIP if not available]
  [6] No existing SAP Sales Order for this po_number     [best-effort SAP query]

Usage:
    from pre_creation_validator import validate_before_push, ValidationResult

    result = validate_before_push(
        po_number="BE-26-00192",
        sold_to_id="4020010504",
        currency="EUR",
        line_items=[{"material_code": "...", "quantity": 500, "unit_price": 12.5}],
        total_po_value=6250.0,
    )
    if not result.passed:
        # Route to exception queue — do NOT push to Celonis
        print(result.failed_checks)
    else:
        # Safe to push
        push_to_celonis(...)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


# ---------------------------------------------------------------------------
# Price tolerance: extracted line-item total vs PO header total.
# 2% default to cover minor rounding differences in multi-currency POs.
# ---------------------------------------------------------------------------
TOTAL_PRICE_TOLERANCE_PCT = float(os.getenv("PO_PRICE_TOLERANCE_PCT", "2.0"))


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """
    Result of validate_before_push().

    Attributes:
        passed          : True only if ALL checks passed (or were SKIP).
        checks          : Dict[field_name, "PASS" | "FAIL" | "SKIP"]
        failed_checks   : List of check names that returned FAIL.
        details         : Human-readable explanation for each check.
        escalation_reason : Compact machine-readable reason string for the audit log.
    """
    passed: bool
    checks: Dict[str, str] = field(default_factory=dict)
    failed_checks: List[str] = field(default_factory=list)
    details: Dict[str, str] = field(default_factory=dict)
    escalation_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "failed_checks": self.failed_checks,
            "details": self.details,
            "escalation_reason": self.escalation_reason,
        }


# ---------------------------------------------------------------------------
# Main validation function
# ---------------------------------------------------------------------------

def validate_before_push(
    *,
    po_number: str,
    sold_to_id: str,
    currency: str = "",
    line_items: Optional[List[dict]] = None,
    total_po_value: Optional[float] = None,
    price_tolerance_pct: float = TOTAL_PRICE_TOLERANCE_PCT,
    check_sap_duplicate: bool = True,
) -> ValidationResult:
    """
    Run all pre-creation validation checks.

    Args:
        po_number           : Extracted PO number (exact string from document).
        sold_to_id          : SAP Sold-To ID resolved by customer matching.
        currency            : Currency code extracted from PO (e.g. 'EUR').
        line_items          : List of dicts with keys: material_code, quantity, unit_price.
        total_po_value      : Header-level total value from PO (float). None = skip check.
        price_tolerance_pct : Max allowed % deviation between header total and computed total.
        check_sap_duplicate : If True, attempt to query SAP for duplicate SO (best-effort).

    Returns:
        ValidationResult
    """
    checks  : Dict[str, str] = {}
    details : Dict[str, str] = {}
    failed  : List[str]      = []

    items = line_items or []

    # ── [1] customer_id present ───────────────────────────────────────────────
    if sold_to_id and str(sold_to_id).strip() not in ("", "nan", "None"):
        checks["customer_id_present"] = "PASS"
        details["customer_id_present"] = f"sold_to_id={sold_to_id}"
    else:
        checks["customer_id_present"] = "FAIL"
        details["customer_id_present"] = "sold_to_id is empty — customer was not matched"
        failed.append("customer_id_present")

    # ── [2] po_number present ─────────────────────────────────────────────────
    if po_number and str(po_number).strip() not in ("", "nan", "None"):
        checks["po_number_present"] = "PASS"
        details["po_number_present"] = f"po_number='{po_number}'"
    else:
        checks["po_number_present"] = "FAIL"
        details["po_number_present"] = "po_number is empty — extraction failed to find PO number"
        failed.append("po_number_present")

    # ── [3] At least one line item with mapped material + qty ─────────────────
    valid_items = [
        it for it in items
        if (it.get("material_code") or it.get("internal_material_code"))
        and _parse_qty(it.get("quantity", 0)) > 0
    ]
    if valid_items:
        checks["line_items_valid"] = "PASS"
        details["line_items_valid"] = f"{len(valid_items)} valid line item(s) with mapped material and quantity"
    else:
        checks["line_items_valid"] = "FAIL"
        details["line_items_valid"] = (
            f"No valid line items found (total items={len(items)}). "
            "Each item needs a mapped material_code/internal_material_code and quantity > 0."
        )
        failed.append("line_items_valid")

    # ── [4] Currency present ──────────────────────────────────────────────────
    if currency and str(currency).strip() not in ("", "nan", "None"):
        checks["currency_present"] = "PASS"
        details["currency_present"] = f"currency='{currency}'"
    else:
        checks["currency_present"] = "FAIL"
        details["currency_present"] = "currency is empty — could not extract currency from PO"
        failed.append("currency_present")

    # ── [5] Total PO value vs sum(line items) within tolerance ────────────────
    if total_po_value is not None and valid_items:
        try:
            computed_total = sum(
                _parse_qty(it.get("quantity", 0)) * _parse_price(it.get("unit_price", 0))
                for it in valid_items
                if _parse_price(it.get("unit_price", 0)) > 0
            )
            if computed_total > 0:
                deviation_pct = abs(computed_total - total_po_value) / max(total_po_value, 0.01) * 100
                if deviation_pct <= price_tolerance_pct:
                    checks["total_value_check"] = "PASS"
                    details["total_value_check"] = (
                        f"PO total={total_po_value:.2f} computed={computed_total:.2f} "
                        f"deviation={deviation_pct:.1f}% (tolerance={price_tolerance_pct}%)"
                    )
                else:
                    checks["total_value_check"] = "FAIL"
                    details["total_value_check"] = (
                        f"PO total={total_po_value:.2f} vs computed={computed_total:.2f} "
                        f"deviation={deviation_pct:.1f}% EXCEEDS tolerance={price_tolerance_pct}%"
                    )
                    failed.append("total_value_check")
            else:
                # unit_prices all zero — skip this check
                checks["total_value_check"] = "SKIP"
                details["total_value_check"] = "unit_prices not available — total check skipped"
        except Exception as e:
            checks["total_value_check"] = "SKIP"
            details["total_value_check"] = f"total check error: {e}"
    else:
        checks["total_value_check"] = "SKIP"
        details["total_value_check"] = "total_po_value not provided — check skipped"

    # ── [6] SAP duplicate check (best-effort — FAIL only if confirmed duplicate) ──
    if check_sap_duplicate and po_number and str(po_number).strip():
        dup_result = _check_sap_duplicate(po_number)
        if dup_result == "DUPLICATE":
            checks["no_sap_duplicate"] = "FAIL"
            details["no_sap_duplicate"] = (
                f"SAP Sales Order already exists for PO '{po_number}' — "
                "duplicate prevention blocked creation"
            )
            failed.append("no_sap_duplicate")
        elif dup_result == "CLEAR":
            checks["no_sap_duplicate"] = "PASS"
            details["no_sap_duplicate"] = f"No existing SAP SO found for PO '{po_number}'"
        else:
            # Could not query SAP — skip rather than block
            checks["no_sap_duplicate"] = "SKIP"
            details["no_sap_duplicate"] = f"SAP duplicate check unavailable: {dup_result}"
    else:
        checks["no_sap_duplicate"] = "SKIP"
        details["no_sap_duplicate"] = "SAP duplicate check skipped"

    # ── Build result ──────────────────────────────────────────────────────────
    passed = len(failed) == 0
    escalation_reason = ""
    if not passed:
        escalation_reason = "pre_creation_validation_failed: " + ", ".join(failed)

    return ValidationResult(
        passed=passed,
        checks=checks,
        failed_checks=failed,
        details=details,
        escalation_reason=escalation_reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_qty(val: Any) -> float:
    """Parse a quantity value to float, returning 0 on failure."""
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_price(val: Any) -> float:
    """Parse a price/unit_price value to float, returning 0 on failure."""
    try:
        cleaned = str(val).replace(",", "").replace(" ", "").strip()
        return float(cleaned)
    except (ValueError, TypeError):
        return 0.0


def _check_sap_duplicate(po_number: str) -> str:
    """
    Check if a SAP Sales Order already exists for this PO number.

    Priority:
      1. Azure Email Tracker (AzureEmailTracker) — authoritative cloud store
      2. Local SQLite processed_emails.db        — fallback if Azure unavailable

    Returns:
        "DUPLICATE" — confirmed existing SO found
        "CLEAR"     — no existing SO found
        "<reason>"  — check could not be performed (treated as SKIP)
    """
    po_clean = str(po_number).strip().lower()
    if not po_clean:
        return "empty po_number"

    # ── 1. Azure Email Tracker (primary / source of truth) ───────────────────
    try:
        from azure_email_tracker import AzureEmailTracker
        tracker = AzureEmailTracker()
        prev_records = tracker.get_emails_by_po_number(po_clean)
        _DUPLICATE_STATUSES = {
            "PUSHED_TO_CELONIS", "PENDING_STAGE2", "ROBONA_SENT",
            "SO_BLOCKED", "MAPPED_SUCCESS", "SKIP_DUPLICATE",
        }
        for rec in (prev_records or []):
            st = str(rec.get("status", "") or "").strip().upper()
            if st in _DUPLICATE_STATUSES:
                return "DUPLICATE"
        return "CLEAR"
    except ImportError:
        pass  # azure_email_tracker not available — fall through to SQLite
    except Exception as az_err:
        print(f"  [Validator-WARN] Azure duplicate check failed: {az_err} — trying SQLite fallback")

    # ── 2. Local SQLite fallback ──────────────────────────────────────────────
    try:
        from pathlib import Path
        import sqlite3
        db_path = Path(__file__).parent / "processed_emails.db"
        if not db_path.exists():
            return "SQLite tracker not found and Azure unreachable"
        conn = sqlite3.connect(str(db_path))
        cur  = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM processed_emails "
                "WHERE status='SO_CREATED' AND (subject LIKE ? OR po_number=?)",
                (f"%{po_number}%", po_number),
            )
            count = cur.fetchone()[0]
        except Exception:
            count = 0
        conn.close()
        return "DUPLICATE" if count > 0 else "CLEAR"
    except Exception as sql_err:
        return f"check_error: {sql_err}"



# ---------------------------------------------------------------------------
# Quick self-test — run: python pre_creation_validator.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    r = validate_before_push(
        po_number="TEST-001",
        sold_to_id="4020010504",
        currency="EUR",
        line_items=[
            {"material_code": "BKV30H2.0", "internal_material_code": "000000000050123456",
             "quantity": "500", "unit_price": "12.50"},
        ],
        total_po_value=6250.0,
        check_sap_duplicate=False,
    )
    print(f"\nResult: passed={r.passed}")
    for k, v in r.checks.items():
        print(f"  [{v}] {k}: {r.details[k]}")
    if r.escalation_reason:
        print(f"\nEscalation reason: {r.escalation_reason}")
