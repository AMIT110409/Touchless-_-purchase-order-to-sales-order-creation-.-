"""
csr_routing.py
==============
3-Tier CSR Exception Email Routing Logic for Envalior

Tier 1 (Direct CSR / Employee Responsible):
  - Primary (To): Employee Responsible email (from CMIR / Customer Master)
  - Secondary (CC): Regional Mailbox (e.g. CustomerCare-EU@envalior.com)

Tier 2 (Sales Org -> Regional Mailbox):
  - Primary (To): Regional Mailbox mapped to Sales Org (e.g. Sales Org 2545 -> CustomerCare-EU@envalior.com)
  - Secondary (To): Optional secondary regional email (e.g. Sales Org 2710 -> CSR-Japan & CSR-GC)

Tier 3 (Fuzzy Region Fallback / All Regional Mailboxes):
  - If Sales Org & Customer are unknown:
    - Attempt fuzzy infer region from address, VAT prefix, or country text in header/body
    - If fuzzy match succeeds -> Primary (To): Inferred Regional Mailbox
    - If fuzzy match fails -> Primary (To): ALL 6 Regional Mailboxes
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, List, Tuple

REGIONAL_CONFIG_PATH = Path(__file__).parent / "regional_config.json"

# All official Envalior regional mailboxes (for Tier 3 fallback when region cannot be identified)
ALL_REGIONAL_MAILBOXES = [
    "CustomerCare-EU@envalior.com",
    "CustomerCare-EU05@envalior.com",
    "customerservice.dem-am@envalior.com",
    "CSR-GC@envalior.com",
    "CSR-KR@envalior.com",
    "CS-India.Team@envalior.com",
    "CSR-Japan@envalior.com",
]


def load_regional_config() -> dict:
    """Load regional configuration from regional_config.json."""
    if REGIONAL_CONFIG_PATH.exists():
        try:
            with open(REGIONAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"  [WARN] Failed to load regional_config.json: {e}")
    return {}


def infer_region_from_text(header_fields: Optional[dict] = None, text_content: Optional[str] = None) -> Optional[str]:
    """
    Tier 3 Fuzzy Logic: Infer region key (EU, AMS, CN_GC, JP, KR, IN) from
    address, city, country, or VAT prefix.
    """
    blob = ""
    if header_fields:
        blob += " " + " ".join([
            str(header_fields.get("country", "")),
            str(header_fields.get("address", "")),
            str(header_fields.get("city", "")),
            str(header_fields.get("vat_number", "")),
            str(header_fields.get("ship_to_address", "")),
            str(header_fields.get("sold_to_address", "")),
        ])
    if text_content:
        blob += " " + str(text_content)

    blob_lower = blob.lower().strip()
    if not blob_lower:
        return None

    # VAT Prefixes & Countries for EU
    eu_indicators = [
        "vat: de", "vat: fr", "vat: it", "vat: es", "vat: nl", "vat: be", "vat: pl", "vat: pt",
        "vat: at", "vat: se", "vat: fi", "vat: dk", "vat: cz", "vat: hu", "vat: ro", "vat: bg",
        "germany", "deutschland", "france", "italy", "italia", "spain", "españa", "netherlands",
        "belgium", "austria", "portugal", "poland", "sweden", "denmark", "finland", "ireland",
        "united kingdom", "uk", "switzerland", "norway", "europe", "eu"
    ]
    if any(k in blob_lower for k in eu_indicators):
        return "EU"

    # Americas (US / MX / BR / CA)
    ams_indicators = [
        "usa", "united states", "america", "mexico", "méxico", "brazil", "brasil",
        "canada", "us", "mx", "br", "ca"
    ]
    if any(k in blob_lower for k in ams_indicators):
        return "AMS"

    # Japan
    if any(k in blob_lower for k in ["japan", "tokyo", "osaka", "yokohama", "jp"]):
        return "JP"

    # Korea
    if any(k in blob_lower for k in ["korea", "seoul", "busan", "kr"]):
        return "KR"

    # India
    if any(k in blob_lower for k in ["india", "mumbai", "delhi", "bangalore", "chennai", "in"]):
        return "IN"

    # China / Greater China / APAC
    cn_indicators = [
        "china", "shanghai", "beijing", "guangzhou", "shenzhen", "hong kong",
        "taiwan", "singapore", "vietnam", "thailand", "cn", "sg", "tw", "hk"
    ]
    if any(k in blob_lower for k in cn_indicators):
        return "CN_GC"

    return None


def resolve_csr_routing(
    sales_org: Optional[str] = None,
    customer_number: Optional[str] = None,
    salesperson_email: Optional[str] = None,
    header_fields: Optional[dict] = None,
    text_content: Optional[str] = None,
    regional_config: Optional[dict] = None,
) -> Tuple[List[str], List[str], str, str]:
    """
    3-Tier CSR Exception Email Routing Logic:

    Returns:
        (to_recipients, cc_recipients, region_name, tier_description)
    """
    if regional_config is None:
        regional_config = load_regional_config()

    # Determine region from Sales Org if provided
    matched_region_key = None
    region_info = {}
    if sales_org:
        for r_key, r_cfg in regional_config.items():
            if sales_org in r_cfg.get("sales_orgs", []):
                matched_region_key = r_key
                region_info = r_cfg
                break

    regional_cs_email = region_info.get("cs_email")
    secondary_email = region_info.get("secondary_email")
    region_name = region_info.get("region_name", matched_region_key or "UNKNOWN")

    to_recipients = []
    cc_recipients = []
    tier_desc = ""

    # ── Tier 1: Employee Responsible (Specific CSR) ─────────────────────────
    if salesperson_email and "@" in str(salesperson_email):
        clean_csr_email = str(salesperson_email).strip()
        to_recipients.append(clean_csr_email)

        # Keep regional mailbox in CC
        if regional_cs_email and regional_cs_email not in to_recipients:
            cc_recipients.append(regional_cs_email)

        tier_desc = f"Tier 1: Direct CSR ({clean_csr_email}) — Regional mailbox in CC"

    # ── Tier 2: Sales Org -> Regional Mailbox ────────────────────────────────
    elif matched_region_key and regional_cs_email:
        to_recipients.append(regional_cs_email)
        if secondary_email and secondary_email not in to_recipients:
            to_recipients.append(secondary_email)

        tier_desc = f"Tier 2: Regional Mailbox via Sales Org {sales_org} ({', '.join(to_recipients)})"

    # ── Tier 3: Fuzzy Infer Region or Send to ALL Regional Mailboxes ────────
    else:
        inferred_region_key = infer_region_from_text(header_fields, text_content)
        if inferred_region_key and inferred_region_key in regional_config:
            inferred_cfg = regional_config[inferred_region_key]
            inferred_email = inferred_cfg.get("cs_email")
            if inferred_email:
                to_recipients.append(inferred_email)
                if inferred_cfg.get("secondary_email"):
                    to_recipients.append(inferred_cfg["secondary_email"])
                tier_desc = f"Tier 3: Inferred Region ({inferred_region_key}) -> {', '.join(to_recipients)}"
                region_name = inferred_region_key

        if not to_recipients:
            # Ultimate Fallback: Unidentified Customer & Region -> Send to ALL Regional Mailboxes
            to_recipients = list(ALL_REGIONAL_MAILBOXES)
            tier_desc = "Tier 3: Unidentified Customer & Region -> Sent to ALL Regional Mailboxes"
            region_name = "ALL_REGIONS"

    # ── Test Email Injection ────────────────────────────────────────────────
    # Always ensure test email (a.rathore@ofiservices.com) is in CC for testing
    test_email = os.getenv("TEST_CS_EMAIL", "a.rathore@ofiservices.com")
    if test_email and "@" in test_email:
        if test_email not in to_recipients and test_email not in cc_recipients:
            cc_recipients.append(test_email)

    return to_recipients, cc_recipients, region_name, tier_desc
