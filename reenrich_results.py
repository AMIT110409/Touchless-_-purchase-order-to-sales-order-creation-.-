"""
Re-Enrichment Script
--------------------
Takes the existing results_po_examples.jsonl (which has OCR + LLM extraction done)
and re-applies the corrected KB fuzzy matching + Excel mapping.

Data source: Azure Blob Storage (Test MP parquet via SalesOrderMapper.from_azure()).
No SQLite / knowledge_base.db dependency.

Azure deployment:
  Set FORCE_FRESH_DATA=true to always download latest Celonis data from Azure Blob.
  Default (false) uses a 24h local parquet cache for faster local development.
"""

import argparse
import json
import os
import re
import sys
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from difflib import SequenceMatcher
from sales_order_mapper import SalesOrderMapper

# -------------------------------------------------------------------
# Generic company name normalisation — legal entity suffix list.
# -------------------------------------------------------------------
# Compiled once at import time for performance.
# Used by _normalize_for_match() to strip suffixes from BOTH the
# extracted PO name AND every vendor name in the Celonis cache before
# SequenceMatcher comparison.
#
# Effect: 'Planet Asia Pte Ltd' and 'Planet Asia Pte Ltd. Planet Asia Pte Ltd.'
# both normalise to 'planet asia' → score 1.0 with no manual alias needed.
# Adding new Envalior customers with any legal suffix just works automatically.
_LEGAL_SUFFIXES_RE = re.compile(
    r'(?i)\b(?:'
    # ── English ───────────────────────────────────────────────────────
    r'limited|ltd\.?|llc\.?|l\.l\.c\.?|inc\.?|incorporated'
    r'|corp\.?|corporation|co\.?|company|plc\.?|llp\.?|lp\.?'
    r'|partnerships?'
    # ── Asian: Singapore / HK / MY / IN ───────────────────────────────
    r'|pte\.?\s*ltd\.?|pte\.?|pvt\.?\s*ltd\.?|pvt\.?'
    # ── German / Austrian / Swiss ─────────────────────────────────────
    r'|gmbh\.?|ag\.?|kg\.?|ohg\.?|kgaa\.?|se\.?|ug\.?'
    # ── Italian ───────────────────────────────────────────────────────
    r'|s\.?\s*r\.?\s*l\.?|srl\.?|s\.?\s*p\.?\s*a\.?|spa\.?'
    # ── French / Spanish / Portuguese ─────────────────────────────────
    r'|s\.?\s*a\.?\s*s\.?|sas\.?|sarl\.?|s\.?\s*a\.?\s*r\.?\s*l\.?'
    r'|eurl\.?|sasu\.?|s\.?\s*l\.?\s*u\.?|lda\.?'
    # ── Nordic / Dutch / Belgian ───────────────────────────────────────
    r'|b\.?\s*v\.?|n\.?\s*v\.?|ab\.?|aps\.?|a\.?\s*s\.?|oy\.?'
    # ── Polish / Czech / Slovak ───────────────────────────────────────
    r'|sp\.?\s*z\.?\s*o\.?\s*o\.?|spol\.?\s*s\.?\s*r\.?\s*o\.?'
    r'|s\.?\s*r\.?\s*o\.?'
    r')\s*$'
)

# -------------------------------------------------------------------
# Azure vs local cache control
# -------------------------------------------------------------------
# FORCE_FRESH_DATA=true  → always pull latest from Azure Blob (use on Azure)
# FORCE_FRESH_DATA=false → use 24h local .celonis_cache/ parquet (local dev)
FORCE_FRESH_DATA = os.getenv("FORCE_FRESH_DATA", "false").lower() == "true"

# -------------------------------------------------------------------
# WE = AG detection
# -------------------------------------------------------------------
# In German/SAP logistics shorthand:
#   WE = Warenempfänger (Ship-to party)
#   AG = Auftraggeber   (Sold-to party)
# "WE = AG" on a PO or email means Ship-to == Sold-to (same company,
# same address, same SAP partner). No separate ship-to lookup needed.
_WE_AG_PATTERN = re.compile(
    r'\bWE\s*=\s*AG\b'
    r'|Lieferadresse\s*=\s*Rechnungsadresse'
    r'|ship[- ]?to\s*=\s*sold[- ]?to'
    r'|Warenempfänger\s*=\s*Auftraggeber',
    re.IGNORECASE
)

def _detect_we_equals_ag(header: dict, raw_text: str = "") -> bool:
    """
    Return True if this PO/email instructs that Ship-to == Sold-to.

    Detection sources (in priority order):
      1. LLM-extracted flag:  header["ship_to_same_as_sold_to"] == True
      2. Raw text regex:       _WE_AG_PATTERN matches in email body / PO text
    """
    # 1. LLM flag (set by updated prompt)
    if header.get("ship_to_same_as_sold_to") in (True, "true", "True", 1, "1"):
        return True
    # 2. Regex fallback (works on old records and on email-body-only POs)
    for text in (raw_text, header.get("raw_text", ""), header.get("email_body", "")):
        if text and _WE_AG_PATTERN.search(str(text)):
            return True
    return False

# -------------------------------------------------------------------
# Lazy singleton mapper — loaded once per process.
# All vendor/ship-to/material data comes from this single instance.
# -------------------------------------------------------------------
_mapper_instance = None

def _get_mapper() -> SalesOrderMapper:
    """Return (or create) the shared SalesOrderMapper backed by Azure Blob."""
    global _mapper_instance
    if _mapper_instance is None:
        _mapper_instance = SalesOrderMapper.from_azure(force_refresh=FORCE_FRESH_DATA)
    return _mapper_instance


# -------------------------------------------------------------------
# Vendor cache — backed by Test MP, NOT SQLite
# -------------------------------------------------------------------
_vendor_cache = None

def _load_vendor_cache():
    """Load all vendor (sold_to_id, sold_to_name_full) pairs from Test MP parquet."""
    global _vendor_cache
    if _vendor_cache is not None:
        return _vendor_cache
    raw = _get_mapper().get_vendor_list()
    # Deduplicate doubled names coming from Celonis Test MP storage format
    # e.g. 'NIHON MOLEX NIHON MOLEX' -> 'NIHON MOLEX'
    # This prevents SequenceMatcher scores being penalised by the repetition.
    deduped = []
    for vid, vname in raw:
        # ── Step 1: Undo Celonis doubled-name storage artifact ─────────────
        # Celonis Test MP stores names as 'ACME GmbH ACME GmbH' (exact repeat)
        # OR as 'ACME GmbH ACME GmbHStreet Address...' (address appended to 2nd copy).
        # Strategy A: exact-half dedup (handles 'NIHON MOLEX NIHON MOLEX')
        # Strategy B: regex-based prefix dedup (handles 'M & Q Packaging Ltd M & Q Packaging LtdUnit 5...')
        stripped = vname.strip()
        parts = stripped.split()
        half  = len(parts) // 2
        deduped_name = stripped  # default: keep as-is
        if half >= 2 and parts[:half] == parts[half:]:
            # Strategy A: exact word-level repeat
            deduped_name = " ".join(parts[:half])
        else:
            # Strategy B: find the longest prefix P such that the string starts with P+P
            # Works even when extra address text follows the second copy.
            # We test successively shorter word-prefixes until we find a match.
            words = stripped.split()
            found = False
            for n in range(len(words) // 2, 1, -1):
                prefix = " ".join(words[:n])
                # Check if the name starts with the prefix repeated (possibly with extra text)
                repeat_start = len(prefix) + 1  # +1 for the space separator
                if stripped.startswith(prefix + " " + prefix):
                    deduped_name = prefix
                    found = True
                    break
            # if no repeat found, keep the original stripped name

        # ── Step 2: Strip trailing punctuation ────────────────────────────
        # Celonis occasionally appends a trailing dot ('Planet Asia Pte Ltd.')
        deduped_name = deduped_name.rstrip('.,;: ')
        deduped.append((vid, deduped_name))
    _vendor_cache = deduped
    return _vendor_cache


# -------------------------------------------------------------------
# Customer name aliases
# -------------------------------------------------------------------
CUSTOMER_ALIASES = {
    # Niederwieser GmbH (SAP 4020000825, Kreditoren Nr. K00162)
    # The ship-to/warehouse is 'Hauptlager - VF Verpackungen GmbH' (same company, same SAP ID).
    # All variants must resolve to 'Niederwieser GmbH' which IS in the Test MP.
    "niederwieser": "Niederwieser GmbH",
    "hauptlager - niederwieser gmbh": "Niederwieser GmbH",
    "hauptlager - niederwieser": "Niederwieser GmbH",
    "vf verpackungen": "Niederwieser GmbH",          # VF Verpackungen = Niederwieser warehouse
    "hauptlager - vf verpackungen": "Niederwieser GmbH",
    "hauptlager vf verpackungen": "Niederwieser GmbH",
    "bando chemical industries": "BANDO KAGAKU",
    "ecoform multifol": "Ecoform Multifol Verpackungsfolien",
    "mf-folien": "MF Folien GmbH",
    "mf folien": "MF Folien GmbH",
    "molex interconnect": "上海莫仕连接器有限公司",
    "molex interconnect (shanghai)": "上海莫仕连接器有限公司",
    "molex interconnect (shanghai) co": "上海莫仕连接器有限公司",
    "molex japan": "NIHON MOLEX",
    "molex japan llc": "NIHON MOLEX",
    "molex-japan": "NIHON MOLEX",
    "molex-japan llc": "NIHON MOLEX",
    "molex-japan, llc": "NIHON MOLEX",
    "ergotech srl a socio unico": "Ergotech SRL",
    # Parker Hannifin: The French subsidiary sends POs but the SAP Sold-to is the EMEA entity.
    # customer_name_matched must resolve to the name in Test MP: 'Parker Hannifin EMEA Sarl'
    "parker hannifin manufacturing france sas": "Parker Hannifin EMEA S\u00e0rl",
    "parker hannifin manufacturing france": "Parker Hannifin EMEA S\u00e0rl",
    "parker hannifin france": "Parker Hannifin EMEA S\u00e0rl",
    "parker hannifin emea sarl": "Parker Hannifin EMEA S\u00e0rl",
    "parker hannifin emea": "Parker Hannifin EMEA S\u00e0rl",
    "parker hannifin": "Parker Hannifin EMEA S\u00e0rl",
    # Bourbon Automotive Plastic — 'AP' is used on POs as abbreviation for 'Automotive Plastic'
    "bourbon ap nitra": "BOURBON Automotive Plastic Nitra",
    "bourbon ap": "BOURBON Automotive Plastic",
    # ARÇELİK — Turkish special chars (Ç, İ, Ş) and full dept name cause low scores.
    # SAP has two records: 4020040514 (main) and 4020023225.
    # Alias to the short SAP name so SequenceMatcher gives a clean score.
    "arçelik": "Arcelik A.S.",
    "arcelik": "Arcelik A.S.",
    "arçelik a.ş": "Arcelik A.S.",
    "arcelik a.s": "Arcelik A.S.",
    # ČEGAN s.r.o. — two distinct SAP customers share a similar name:
    #   4020004125 = Cegan Production s.r.o  (1 material: BG30XH2.0D, no X1022600 CMIR)
    #   4020041175 = CEGAN s.r.o.            (has CMIR X1022600 → B3235 000000 BA100W)
    # POs signed "ČEGAN s.r.o." with article number x1022600 belong to 4020041175.
    # Direct ID alias ensures we bypass name fuzzy-match and pick the right entity.
    "čegan": "4020041175",
    "cegan": "4020041175",
    # Junchuang North America — POs use trade abbreviation 'JC NORTH AMERICA' which starts
    # with 'jc' — 3-char pre-filter blocks SequenceMatcher from even seeing 'Junchuang'.
    "jc north america": "4020039597",
    "junchuang north america": "4020039597",
    "junchuang": "4020039597",
    # ABC Technologies / dlhBOWLES Canton, OH (44706)
    "abc technologies dlhb": "4020015672",
    "dlhbowles": "4020015672",
    "dlh bowles": "4020015672",
    "dlh industries": "4020015672",
    # Scherdel Wiesauplast de México — POs may spell it with/without accent or use 'de Méxi...' (truncated)
    "scherdel wiesauplast de mexico": "4020010062",
    "scherdel wiesauplast de méxico": "4020010062",
    "scherdel wiesauplast mexico": "4020010062",
    # Scherdel Wiesauplast Deutschland — German sister entity
    "scherdel wiesauplast deutschland": "4020025252",
    "scherdel wiesauplast de": "4020025252",
    # Scherer s.r.l. — Italian customer, SalesOrg 2545
    # NOTE: suffix variants ("scherer srl", "scherer s.r.l.") are now handled
    # automatically by the generic _normalize_for_match() legal-suffix stripper.
    # Only the truly non-obvious alias is kept as a safety net.
    "scherer": "4020044050",
    # Planet Asia Pte Ltd — Singapore-based customer, SAP Sold-to 4020038225 (SalesOrg 2780).
    # NOTE: All 'Pte Ltd' suffix variants and the Celonis doubled-name artifact
    # ('Planet Asia Pte Ltd. Planet Asia Pte Ltd.') are now handled automatically
    # by _normalize_for_match() + the dot-stripping in _load_vendor_cache().
    # No explicit alias needed — kept here only as documentation.
    # "planet asia pte ltd": "4020038225",  ← removed: generic logic handles this
    # DEP Engineering / d=p Engineering (Jackson, MI 49202) — trade/short name used on POs.
    # SAP Sold-to: 4020021928 DIVERSIFIED ENGINEERING & PLASTICS, LLC
    # The name on PO headers is 'DEP Engineering' or 'd=p Engineering' (logo abbreviation).
    # Postcode 49202 + material 118208 also confirm the match via fallback, but alias
    # ensures clean resolution even when material OCR fails.
    "dep engineering": "DIVERSIFIED ENGINEERING & PLASTICS, LLC",
    "d=p engineering": "DIVERSIFIED ENGINEERING & PLASTICS, LLC",
    "dep plastics": "DIVERSIFIED ENGINEERING & PLASTICS, LLC",
    "diversified engineering plastics": "DIVERSIFIED ENGINEERING & PLASTICS, LLC",
    # Mitsubishi Chemical Advanced Materials B.V. — Netherlands entity (Almelo, 7602 PK).
    # SAP Sold-to: 4020042625  (self ship-to, same ID).
    # Multiple Mitsubishi Chemical entities in Test MP cause fuzzy scoring to pick the wrong one:
    #   4020005639 "Mitsubishi Chemical Advanced Mat.I" — highest score but WRONG
    #   4020000669 "Mitsubishi Chemical Advanced Materials Belgium NV" — also wrong
    # Direct alias ensures the Netherlands B.V. entity always resolves to 4020042625.
    "mitsubishi chemical advanced materials b.v.": "4020042625",
    "mitsubishi chemical advanced materials bv": "4020042625",
    "mitsubishi chemical advanced materials netherlands": "4020042625",
    "mcam b.v.": "4020042625",
    "mcam bv": "4020042625",
    # Sistemas Técnicos del Accesorio y Componentes S.L. (STAC) — SAP Sold-to 4020044282 (SalesOrg 2540).
    # Master data for 4020044282 contains material STM-MP-DURB29 -> 000000000000050503.
    # Wrong entity 4020020721 (SalesOrg 2500) has no CMIR for STM-MP-DURB29.
    "sistemas tecnicos del accesorio": "4020044282",
    "sistemas técnicos del accesorio": "4020044282",
    "sistemas technicos del accesorio": "4020044282",
    "stac": "4020044282",
    # ── ANTEK-INT Chemical Inc. / 訊知股份有限公司 (Taiwan) ──
    # SAP Sold-to: 4020031290. Material '1010C2' matches 18 customers causing ambiguity.
    # Both the English trade name and romanised Chinese name must resolve here.
    "antek": "4020031290",
    "antek-int": "4020031290",
    "antek int": "4020031290",
    "antek-int chemical": "4020031290",
    "antek int chemical": "4020031290",
    "訊知": "4020031290",
    "訊知股份有限公司": "4020031290",
    # ── Hai Xin Technology (Shenzhen) / 海星科技（深圳）有限公司 ──
    # TWO SAP Sold-To entities exist with this name:
    #   4020028040 = 海星科技(深圳)有限公司  (half-width brackets) - MAIN trading entity
    #   4020038821 = 海星科技（深圳）有限公司  (full-width brackets) - secondary
    # Both have 171-DP2851-000 CMIR. POs use half-width brackets -> 4020028040.
    # The alias catches both half-width and ASCII 'hai xin technology' inputs.
    "hai xin technology": "4020028040",
    "haixintechnology": "4020028040",
    "海星科技(深圳)有限公司": "4020028040",
    "海星科技（深圳）有限公司": "4020038821",
    "海星科技": "4020028040",  # generic short form -> main entity
    # ── INABATA (Vietnam / Singapore / India) ──
    # CMIR for TUFBET BGF30 BLACK is under Sold-to 4020028124 (Inabata & Co., Ltd. Singapore).
    # POs may come from Inabata Vietnam, India, or Singapore — all must map to the SAP Sold-to.
    # ── INABATA SINGAPORE / INABATA VIETNAM ──
    # SAP Sold-to: 4020011631 (INABATA SINGAPORE (PTE.) LTD.)
    # SAP Ship-to: 4020028161 (INABATA VIETNAM CO. LTD.)
    "inabata singapore": "4020011631",
    "inabata singapore (pte.) ltd.": "4020011631",
    "inabata singapore pte ltd": "4020011631",
    "ik inabata singapore": "4020011631",
    "ik inabata singapore (pte.) ltd.": "4020011631",
    "inabata vietnam": "4020028161",
    "inabata vietnam co., ltd": "4020028161",
    "inabata vietnam co ltd": "4020028161",
    # ── TORAY INTERNATIONAL / 東レ インターナショナル ──
    # Japanese POs use Kanji '東レ インターナショナル株式会社' or '東レ' or 'TORAY INTERNATIONAL'
    # SAP Sold-to: 4020034383 (TORAY INTERNATIONAL INC. OSAKA)
    # SAP Ship-to: 4020034385 (TORAY OKAZAKI PLANT)
    "東レ インターナショナル": "4020034383",
    "東レ インターナショナル株式会社": "4020034383",
    "東レ株式会社": "4020034383",
    "東レ": "4020034383",
    "東レ株式会社 岡崎工場": "4020034385",
    "toray international": "4020034383",
    "toray okazaki": "4020034385",
    # ── STAC (Sistemas Técnicos del Accesorio y Componentes S.L.) ──
    # SAP Sold-to: 4020044282 (Sistemas Técnicos del Accesorio y Componentes S.L.)
    # SAP Ship-to: 4020044845 (PORTAL 12 - CATOIRA, postcode 36612)
    "stac": "4020044282",
    "sistemas técnicos del accesorio y componentes": "4020044282",
    "sistemas técnicos del accesorio y componentes s.l.": "4020044282",
    "sistemas tecnicos del accesorio y componentes": "4020044282",
    "sistemas tecnicos del accesorio y componentes s.l.": "4020044282",
    "sistemas técnicos del accesorio y componentes, s.l.": "4020044282",
    "sistemas tecnicos del accesorio y componentes, s.l.": "4020044282",
    "sistemas técnicos del accesorio": "4020044282",
    "sistemas tecnicos del accesorio": "4020044282",
    # ── LOGITAL SRL (SAP Sold-to 4020026106, Ship-to 4020040935/4020023526, CSR Liza Bety / CustomerCare-EU05@envalior.com) ──
    "logital": "4020026106",
    "logital srl": "4020026106",
    "logital s.r.l.": "4020026106",
    "logital srl a socio unico": "4020026106",
    # ── SC DIAPLAST PRODUCTION SRL — Romanian customer, SAP Sold-to 4020045588 (SalesOrg 2540).
    # POs use the Romanian legal prefix 'SC' (Societate Comerciala) before the company name:
    #   'SC DIAPLAST PRODUCTION SRL' → normalises to 'sc diaplast production'
    # The 3-char pre-filter on 'sc ' blocks SequenceMatcher from seeing 'diaplast production'.
    # Direct alias ensures this customer always resolves correctly.
    "sc diaplast production": "4020045588",
    "diaplast production": "4020045588",
    "sc diaplast": "4020045588",
    "diaplast": "4020045588",
}

# -------------------------------------------------------------------
# Material-code-based customer lookup (fallback when name match fails)
# -------------------------------------------------------------------
def _match_customer_by_material(line_items: list, customer_name: str = ""):
    """
    When fuzzy name matching returns no result, try to identify the customer
    by looking up the extracted customer material code in Test MP.

    Works reliably when a material code is UNIQUE to one customer.
    When multiple customers match (ambiguous), uses customer_name to disambiguate.
    Also tries fuzzy material code matching (≥0.82 ratio) for typos like 15/1PA6GF30GR vs 15/PA6GF30GR.
    Returns the same dict format as fuzzy_match_vendor: {id, name, score} or None.
    """
    if not line_items:
        return None
    try:
        import pandas as pd
        cache_path = os.path.join(".celonis_cache", "test_mp_customer_master.parquet")
        mp_df = pd.read_parquet(cache_path)
        cmat_col = "customer_material_number"
        sold_col = "sold_to_id"
        name_col = "sold_to_name_full"

        for item in line_items:
            mat_code = str(item.get("material_code", "") or "").strip()
            if not mat_code or mat_code in ("-", "N/A", "", "None"):
                continue

            # --- Step 1: Exact match ---
            hits = mp_df[mp_df[cmat_col] == mat_code]
            if hits.empty:
                hits = mp_df[mp_df[cmat_col] == mat_code.lstrip("0")]

            # --- Step 2: Fuzzy material code match (handles 1-char typos like 15/1PA6GF30GR → 15/PA6GF30GR) ---
            # Bypass fuzzy matching for purely numeric codes (different digits = completely different parts, not typos)
            is_numeric = "".join(c for c in mat_code if c.isalnum()).isdigit()
            if hits.empty and len(mat_code) >= 6 and not is_numeric:
                cmat_series = mp_df[cmat_col].dropna().astype(str)
                best_idx = None
                best_ratio = 0.0
                for idx, cmat in cmat_series.items():
                    if abs(len(cmat) - len(mat_code)) > 4:
                        continue  # skip clearly different lengths
                    ratio = SequenceMatcher(None, mat_code.lower(), cmat.lower()).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_idx = idx
                if best_ratio >= 0.82 and best_idx is not None:
                    fuzzy_mat = cmat_series[best_idx]
                    print(f"  [KB] Fuzzy material match: '{mat_code}' ~ '{fuzzy_mat}' (ratio={best_ratio:.2f})")
                    hits = mp_df[mp_df[cmat_col] == fuzzy_mat]

            if hits.empty:
                continue

            unique_customers = hits[sold_col].unique()

            if len(unique_customers) == 1:
                sold_to_id = unique_customers[0]
                raw_name = hits[name_col].iloc[0]
                # Deduplicate doubled names (Celonis Test MP storage artifact)
                parts = str(raw_name).strip().split()
                half = len(parts) // 2
                if half >= 2 and parts[:half] == parts[half:]:
                    raw_name = " ".join(parts[:half])
                print(f"  [KB] Material fallback: '{mat_code}' uniquely maps to {sold_to_id} ({raw_name})")
                return {"id": sold_to_id, "name": raw_name, "score": 0.85}

            elif len(unique_customers) > 1:
                # --- Step 3a: Alias-based disambiguation (highest priority) ---
                # If the extracted customer name maps to a known alias and that
                # alias resolves to one of the ambiguous candidates, return directly.
                # This handles cases like 'Antek-Int Chemical Inc.' → 4020031290
                # even when SAP stores the name as Chinese-only '訊知股份有限公司'.
                if customer_name and customer_name.strip():
                    import re as _re
                    _cname_lower = customer_name.strip().lower()
                    _cname_clean = ' '.join(_re.sub(r'[^a-z0-9\s]', ' ', _cname_lower).split())
                    for _alias, _canonical in CUSTOMER_ALIASES.items():
                        # Check CJK aliases via Unicode containment
                        _has_cjk_a = any('\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' for c in _alias)
                        if _has_cjk_a:
                            _alias_matched = (_alias in customer_name or _alias.lower() in customer_name.lower())
                        else:
                            _alias_clean = ' '.join(_re.sub(r'[^a-z0-9\s]', ' ', _alias.lower()).split())
                            _alias_matched = bool(_alias_clean) and (_alias_clean in _cname_clean)
                        if _alias_matched and str(_canonical) in [str(c) for c in unique_customers]:
                            _cid = str(_canonical)
                            _alias_rows = hits[hits[sold_col].astype(str) == _cid]
                            _alias_name = str(_alias_rows[name_col].iloc[0]).strip() if not _alias_rows.empty else _cid
                            print(
                                f"  [KB] Material fallback: '{mat_code}' ambiguous across "
                                f"{len(unique_customers)} customers \u2014 resolved by alias "
                                f"'{customer_name}' \u2192 {_cid}"
                            )
                            return {"id": _cid, "name": _alias_name, "score": 1.0}

                # --- Step 3b: Disambiguate using customer name fuzzy match ---
                if customer_name and customer_name.strip():
                    cname_lower = customer_name.strip().lower()

                    # Normalize: strip non-alphanumeric for robust matching
                    # (é → '' , dots, dashes etc. removed)
                    import re as _re
                    def _alnum(s):
                        return _re.sub(r'[^a-z0-9]', '', s.lower())

                    best_cust_id = None
                    best_cust_name = None
                    best_cust_score = 0.0
                    for cid in unique_customers:
                        cid_rows = hits[hits[sold_col] == cid]
                        raw_n = str(cid_rows[name_col].iloc[0]).strip()
                        # Deduplicate simple doubled names (e.g. 'ABC ABC' -> 'ABC')
                        parts = raw_n.split()
                        half = len(parts) // 2
                        if half >= 2 and parts[:half] == parts[half:]:
                            raw_n = " ".join(parts[:half])
                        # Strategy 1: alphanumeric containment — if extracted name
                        # (alnum-only) appears at the START of the Celonis alnum string,
                        # it's a very strong match even if the full strings differ greatly
                        # (Celonis often appends address data: "Scherdel ... AV. SANTA FE …").
                        alnum_c = _alnum(cname_lower)
                        alnum_r = _alnum(raw_n)
                        if alnum_c and len(alnum_c) >= 8 and alnum_r.startswith(alnum_c):
                            ratio = 0.95
                        elif alnum_c and len(alnum_c) >= 8 and alnum_c in alnum_r[:len(alnum_c) * 2]:
                            ratio = 0.90
                        else:
                            # Strategy 2: SequenceMatcher on FIRST 2× chars of raw_n
                            # (avoids long address tail destroying the ratio)
                            prefix = raw_n[:max(len(cname_lower) * 2, 40)]
                            ratio = SequenceMatcher(None, cname_lower, prefix.lower()).ratio()

                        # Strategy 3 (CJK / multilingual): Unicode character containment.
                        # When the extracted name or SAP name contains CJK/Japanese/Korean
                        # characters, ASCII-only alnum stripping removes them entirely,
                        # making ratio=0. Instead check if the CJK substring of one string
                        # is contained in the other (works for Chinese, Japanese, Korean names).
                        if ratio < 0.60:
                            import unicodedata
                            def _cjk_chars(s):
                                """Extract CJK unified ideograph characters from s."""
                                return ''.join(c for c in s if '\u4e00' <= c <= '\u9fff'
                                               or '\u3040' <= c <= '\u30ff'  # Hiragana/Katakana
                                               or '\uff00' <= c <= '\uffef')  # Fullwidth
                            cjk_extracted = _cjk_chars(customer_name)
                            cjk_raw = _cjk_chars(raw_n)
                            if len(cjk_extracted) >= 2 and cjk_raw:
                                # Extracted CJK is contained in SAP CJK name → strong match
                                if cjk_extracted in cjk_raw or cjk_raw in cjk_extracted:
                                    ratio = max(ratio, 0.88)
                            # Bonus: check if ASCII tokens of customer_name appear in raw_n
                            # e.g. 'ANTEK-INT CHEMICAL INC' tokens ['antek', 'int', 'chemical']
                            # appear in SAP name '訊知股份有限公司 ANTEK-INT CHEMICAL INC.'
                            ascii_tokens = [t for t in _re.split(r'[^a-z]+', cname_lower) if len(t) >= 4]
                            if ascii_tokens:
                                raw_n_lower = raw_n.lower()
                                matched_tokens = sum(1 for t in ascii_tokens if t in raw_n_lower)
                                token_ratio = matched_tokens / len(ascii_tokens)
                                if token_ratio >= 0.6:
                                    ratio = max(ratio, 0.85)

                        if ratio > best_cust_score:
                            best_cust_score = ratio
                            best_cust_id = cid
                            best_cust_name = raw_n
                    if best_cust_score >= 0.60 and best_cust_id:
                        print(
                            f"  [KB] Material fallback: '{mat_code}' ambiguous across "
                            f"{len(unique_customers)} customers — disambiguated by name "
                            f"'{customer_name}' -> {best_cust_id} ({best_cust_name}, score={best_cust_score:.2f})"
                        )
                        return {"id": best_cust_id, "name": best_cust_name, "score": best_cust_score}
                print(f"  [KB] Material fallback: '{mat_code}' matches {len(unique_customers)} customers — ambiguous, skipping")

    except Exception as e:
        print(f"  [WARN] Material fallback lookup failed: {e}")
    return None


def cross_validate_customer_match(
    sold_to_id: str,
    line_items: list = None,
    sales_org: str = "",
    ship_to_postcode: str = "",
    ship_to_city: str = "",
    sold_to_postcode: str = "",
) -> dict:
    """
    After fuzzy-matching a customer name to a sold_to_id, cross-validate
    using other fields extracted from the PO against Test MP data.

    Signals checked (each contributes to confidence score):
      1. Material code overlap    — customer material codes from PO exist in Test MP for this sold_to_id
      2. Ship-to postcode match   — ship-to postcode from PO matches a known ship-to for this sold_to_id
      3. Sales organisation match — sales org from PO matches Test MP for this sold_to_id
      4. City / country hint      — ship-to or sold-to city appears in Test MP records for this sold_to_id

    Returns:
        {
          "confidence":   float (0.0 – 1.0),
          "signals":      dict of each signal result (True/False/None),
          "explanation":  human-readable summary string,
          "warnings":     list of conflict warnings,
        }
    """
    mapper = _get_mapper()
    mp_df = mapper._test_mp_df if hasattr(mapper, "_test_mp_df") else None

    # Try to get the raw Test MP parquet from the mapper's cache
    if mp_df is None:
        try:
            import pandas as pd
            cache_path = mapper._cache_path if hasattr(mapper, "_cache_path") else None
            if cache_path is None:
                import os
                cache_path = os.path.join(".celonis_cache", "test_mp_customer_master.parquet")
            mp_df = pd.read_parquet(cache_path)
        except Exception:
            return {"confidence": 0.5, "signals": {}, "explanation": "Test MP unavailable for cross-validation", "warnings": []}

    # Filter to rows for this sold_to_id
    cust_rows = mp_df[mp_df["sold_to_id"] == sold_to_id]
    if cust_rows.empty:
        return {"confidence": 0.3, "signals": {}, "explanation": f"sold_to_id {sold_to_id} not in Test MP", "warnings": [f"Unknown sold_to_id {sold_to_id}"]}

    signals   = {}
    hits      = []
    misses    = []
    warnings  = []

    # --- Signal 1: Material code overlap ---
    if line_items:
        has_match = False
        mismatch_mats = []
        for item in line_items:
            m_code = str(item.get("material_code") or item.get("extracted_material_number") or item.get("customer_material_number") or "").strip()
            m_desc = str(item.get("material_description") or "").strip()
            if not m_code and not m_desc:
                continue
            
            # Use mapper's smart lookup logic to see if this material maps directly to this customer.
            mapped = mapper.lookup(sold_to_id, m_code, m_desc)
            if mapped and not mapped.get("is_global_fallback"):
                has_match = True
                hits.append(f"material '{m_code or m_desc}' confirmed in Test MP")
            else:
                mismatch_mats.append(m_code or m_desc)

        signals["material_match"] = has_match
        if not has_match and line_items:
            misses.append(f"material(s) {', '.join(mismatch_mats[:3])} NOT found in Test MP for this customer")
            warnings.append(f"Material mismatch: {', '.join(mismatch_mats[:3])} not in Test MP for {sold_to_id}")

    # --- Signal 2: Ship-to postcode match ---
    if ship_to_postcode:
        clean_pc = re.sub(r"\D", "", ship_to_postcode.strip())
        mp_postcodes = set()
        for v in cust_rows["ship_to_postcode"].dropna():
            mp_postcodes.update(re.sub(r"\D", "", str(w)) for w in str(v).split())
        postcode_hit = any(clean_pc and (clean_pc in pc or pc in clean_pc) for pc in mp_postcodes if pc)
        signals["postcode_match"] = postcode_hit
        if postcode_hit:
            hits.append(f"ship-to postcode {ship_to_postcode} found in Test MP for this customer")
        else:
            misses.append(f"ship-to postcode {ship_to_postcode} not in Test MP for this customer")

    # --- Signal 3: Sales organisation match ---
    if sales_org:
        mp_sales_orgs = set(str(x).strip() for x in cust_rows["sales_organization"].dropna() if x)
        sales_org_hit = sales_org.strip() in mp_sales_orgs
        signals["sales_org_match"] = sales_org_hit
        if sales_org_hit:
            hits.append(f"sales_org {sales_org} confirmed in Test MP for this customer")
        else:
            misses.append(f"sales_org {sales_org} not in Test MP for this customer")
            warnings.append(f"Sales org mismatch: PO has {sales_org}, Test MP has {mp_sales_orgs}")

    # --- Signal 4: City / country hint ---
    city_hint = (ship_to_city or sold_to_postcode or "").lower().strip()
    if city_hint:
        mp_cities = " ".join(
            str(x).lower() for x in
            list(cust_rows["ship_to_city"].dropna()) + list(cust_rows["sold_to_city"].dropna())
        )
        # Check if any word from city_hint appears in mp_cities
        city_words = [w for w in re.split(r"[\s,]+", city_hint) if len(w) >= 4]
        city_hit = any(w in mp_cities for w in city_words)
        signals["city_match"] = city_hit
        if city_hit:
            hits.append(f"city hint '{city_hint[:30]}' matches Test MP for this customer")
        else:
            misses.append(f"city hint '{city_hint[:30]}' not in Test MP for this customer")

    # --- Compute overall confidence ---
    checked = [v for v in signals.values() if v is not None]
    if not checked:
        confidence = 0.6   # name matched but nothing else to check
        explanation = "Name matched — no other PO fields available to cross-validate"
    else:
        n_hits = sum(1 for v in checked if v)
        confidence = round(0.5 + 0.5 * (n_hits / len(checked)), 3)
        parts = []
        if hits:
            parts.append("[+] " + "; ".join(hits))
        if misses:
            parts.append("[-] " + "; ".join(misses))
        explanation = " | ".join(parts) if parts else "No signals matched"

    return {
        "confidence":  confidence,
        "signals":     signals,
        "explanation": explanation,
        "warnings":    warnings,
    }


_TRAD_TO_SIMP_CHARS = str.maketrans({
    '廠': '厂', '倉': '仓', '貨': '货', '發': '发', '東': '东',
    '灣': '湾', '臺': '台', '國': '国', '華': '华', '寶': '宝',
    '興': '兴', '實': '实', '業': '业', '體': '体', '組': '组',
    '設': '设', '備': '备', '統': '统', '產': '产', '廣': '广',
    '聯': '联', '環': '环', '創': '创', '術': '术', '訊': '讯',
    '（': '(', '）': ')', '：': ':', '；': ';', '，': ',',
})


# Leading legal-entity prefixes to strip from the START of a company name.
# These are country-specific abbreviations placed BEFORE the actual company name
# (e.g. Romanian 'SC' = Societate Comerciala, Ukrainian 'TOV', etc.).
# They confuse the 3-char pre-filter in fuzzy_match_vendor because the first 3
# chars of the normalised name belong to the prefix, not the company name.
_LEADING_PREFIX_RE = re.compile(
    r'^(?i)(?:'
    # ── Romanian ──────────────────────────────────────────────────────────
    r'sc\s+'          # SC = Societate Comerciala (e.g. 'SC DIAPLAST PRODUCTION SRL')
    r'|ra\s+'         # RA = Regie Autonoma
    r'|sn\s+'         # SN = Societate Nationala
    # ── Ukrainian / Russian ───────────────────────────────────────────────
    r'|tov\s+'        # TOV = LLC (Ukrainian)
    r'|pao\s+'        # PAO = JSC (Russian)
    r'|zao\s+'        # ZAO = CJSC (Russian)
    r')'
)


def _normalize_for_match(text: str) -> str:
    """
    Normalize a company name for fuzzy matching.  Applied to BOTH the
    extracted PO name AND every vendor name in the Celonis cache before
    SequenceMatcher comparison.

    Steps:
      1. Translate Traditional Chinese -> Simplified Chinese & normalize punctuation
      2. Strip diacritics (NFD) — Turkish Ç→C, Czech Č→C, French é→e …
      3. Lowercase + collapse whitespace
      4. Strip leading country-specific legal prefixes (e.g. Romanian 'SC ')
      5. Strip leading/trailing punctuation (dots, commas)
      6. Strip legal entity suffixes iteratively (Pte Ltd, GmbH, S.r.l. …)
    """
    import unicodedata
    if not text:
        return ""
    # Step 1 — Traditional Chinese -> Simplified Chinese & Chinese punctuation
    text = text.translate(_TRAD_TO_SIMP_CHARS)
    # Step 2 — strip diacritics
    nfkd = unicodedata.normalize("NFD", text)
    cleaned = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    # Step 3 — lowercase + collapse whitespace
    cleaned = " ".join(cleaned.lower().split())
    # Dedupe exact repeating half/prefix (Celonis storage artifact: '海星科技(深圳)有限公司 海星科技(深圳)有限公司')
    parts = cleaned.split()
    half = len(parts) // 2
    if half >= 1 and parts[:half] == parts[half:]:
        cleaned = " ".join(parts[:half])
    # Step 4 — strip leading country-specific legal entity prefixes (e.g. 'sc ' for Romanian)
    cleaned = _LEADING_PREFIX_RE.sub('', cleaned).strip()
    # Step 5 — strip surrounding punctuation
    cleaned = cleaned.strip('.,;:')
    # Step 6 — strip legal suffixes iteratively
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _LEGAL_SUFFIXES_RE.sub('', cleaned).strip().strip('.,;:')
    # Final whitespace collapse
    return " ".join(cleaned.split())



# ---------------------------------------------------------------------------
# Confidence thresholds for escalation guard
# ---------------------------------------------------------------------------
# FUZZY_ESCALATION_THRESHOLD: if the best name score is BELOW this value after
# all disambiguation (material + city/postcode), the match is too uncertain to
# auto-proceed — route to human review queue instead.
FUZZY_ESCALATION_THRESHOLD = float(os.getenv("FUZZY_ESCALATION_THRESHOLD", "0.85"))

# FUZZY_AMBIGUITY_WINDOW: if 2+ candidates score within this range of each
# other after all disambiguation, we still cannot confidently pick one — escalate.
FUZZY_AMBIGUITY_WINDOW = float(os.getenv("FUZZY_AMBIGUITY_WINDOW", "0.08"))


def fuzzy_match_vendor(extracted_name, threshold=0.65, line_items=None, address_hint: str = "",
                       escalation_threshold: float = None):

    """
    Fuzzy-match an extracted vendor/customer name to a known customer ID.

    Disambiguation priority:
      1. Exact numeric ID match
      2. Fuzzy name score (SequenceMatcher)
      3. Material overlap (from Test MP — no SQLite)
      4. City/postcode match (from Test MP — no SQLite)
      5. Escalation guard  — if still ambiguous or score too low, return escalation dict
         instead of auto-picking. Caller must check result.get("escalation_required").

    Args:
        extracted_name:       The vendor/customer name text extracted from PO.
        threshold:            Minimum fuzzy score to consider a match.
        line_items:           List of line item dicts (for material-based disambiguation).
        address_hint:         Free-text from PO address fields (for city/country disambiguation).
        escalation_threshold: Score below which a match is treated as low-confidence and
                              escalated to human review. Defaults to FUZZY_ESCALATION_THRESHOLD.

    Returns:
        dict with keys {id, name, score}              — confident unique match
        dict with key  {escalation_required: True, …} — ambiguous / low-confidence: STOP, escalate
        None                                          — no match found at all
    """
    if not extracted_name or len(extracted_name) < 3:
        return None

    _esc_threshold = escalation_threshold if escalation_threshold is not None else FUZZY_ESCALATION_THRESHOLD

    # Normalise diacritics first (Turkish Ç, Czech Č, French accents etc.)
    # This ensures the 3-char pre-filter and SequenceMatcher work on ASCII-clean text
    norm_name = _normalize_for_match(extracted_name)

    # Apply customer aliases (run on both original and normalised form)
    lookup_name = extracted_name
    for raw_input in (extracted_name, norm_name):
        ext_lower = raw_input.lower().strip()
        ext_clean = re.sub(r'[^a-z0-9\s]', ' ', ext_lower)
        ext_clean = ' '.join(ext_clean.split())
        alias_matched = False
        for alias, canonical in CUSTOMER_ALIASES.items():
            # ─── CJK-aware alias matching ──────────────────────────────────────
            # re.sub(r'[^a-z0-9\s]',...) removes ALL unicode chars including CJK,
            # so Chinese/Japanese alias keys like '海星科技' become empty strings.
            # Detect CJK aliases and match them via direct Unicode substring check.
            _has_cjk = any('\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' for c in alias)
            if _has_cjk:
                if alias not in raw_input and alias.lower() not in raw_input.lower():
                    continue
                alias_matched = True
            else:
                alias_clean = re.sub(r'[^a-z0-9\s]', ' ', alias.lower())
                alias_clean = ' '.join(alias_clean.split())
                if not alias_clean or alias_clean not in ext_clean:
                    continue
                alias_matched = True
            # ─── Resolve canonical value ───────────────────────────────────────
            # Canonical may be a SAP sold-to ID (10-digit number like '4020031290')
            # OR a display name string.  IDs → direct vendor lookup; names → rename.
            if alias_matched:
                is_id = (str(canonical).isdigit() or
                         (len(str(canonical)) == 10 and str(canonical).startswith('40')))
                if is_id:
                    target_id = str(canonical)
                    # Step 1: Try vendor cache
                    vendors = _load_vendor_cache()
                    for vendor_id, vendor_name in (vendors or []):
                        if vendor_id == target_id:
                            return {"name": vendor_name, "id": vendor_id, "score": 1.0}
                    # Step 2: Vendor cache miss — look up directly in mapper raw Test MP
                    try:
                        _m = _get_mapper()
                        _raw = getattr(_m, '_df_mp_raw', None)
                        if _raw is not None and 'sold_to_id' in _raw.columns:
                            _rows = _raw[_raw['sold_to_id'].astype(str).str.strip() == target_id]
                            if not _rows.empty:
                                _name = str(_rows['sold_to_name_full'].iloc[0]).strip()
                                return {"name": _name, "id": target_id, "score": 1.0}
                    except Exception:
                        pass
                    # Step 3: Return with ID as name (last resort)
                    return {"name": target_id, "id": target_id, "score": 1.0}
                lookup_name = canonical
                break
        if alias_matched:
            break  # alias matched, stop



    vendors = _load_vendor_cache()
    if not vendors:
        return None

    # Compute the fully normalised form (diacritics + legal suffixes stripped)
    # used for BOTH the exact-ID check and the fuzzy name matching below.
    norm_extracted = _normalize_for_match(lookup_name)

    # -- Exact ID Match (numeric) --
    if sum(c.isdigit() for c in norm_extracted) > len(norm_extracted) / 2:
        clean_extracted_id = "".join(filter(str.isdigit, norm_extracted))
        for vendor_id, vendor_name in vendors:
            if vendor_id and vendor_id.endswith(clean_extracted_id):
                return {"name": vendor_name, "id": vendor_id, "score": 1.0}

    # -- Fuzzy Name Match (compare on fully normalised names) --
    # Both sides go through _normalize_for_match which strips legal suffixes,
    # diacritics, and trailing punctuation.  This means 'Planet Asia Pte Ltd'
    # and 'Planet Asia Pte Ltd. Planet Asia Pte Ltd.' both become 'planet asia'
    # and score 1.0 without any hardcoded alias.
    matches = []
    for vendor_id, vendor_name in vendors:
        v_norm = _normalize_for_match(vendor_name)      # suffix-stripped vendor name
        # 3-char pre-filter on normalised strings to skip obviously unrelated names fast
        if norm_extracted[:3] and norm_extracted[:3] not in v_norm:
            continue
        score = SequenceMatcher(None, norm_extracted, v_norm).ratio()
        if score >= threshold:
            matches.append({"name": vendor_name, "id": vendor_id, "score": round(score, 3)})


    if not matches:
        return None

    matches.sort(key=lambda x: x["score"], reverse=True)
    best_score = matches[0]["score"]
    best_matches = [m for m in matches if m["score"] >= best_score - 0.08]

    if len(best_matches) == 1:
        return best_matches[0]

    mapper = _get_mapper()

    # -- Disambiguation Step 1: Material overlap (Test MP, no SQLite) --
    # po_materials is built for backward compatibility / checks, but we perform
    # a direct lookup verify for each item on the candidate customer.
    if line_items:
        best_match_vendor = None
        max_matches = -1
        for candidate in best_matches:
            match_count = 0
            for item in line_items:
                m_code = item.get("material_code", "")
                m_desc = item.get("material_description", "")
                # Check if this candidate customer has a direct mapping (no global fallback)
                mapped = mapper.lookup(candidate["id"], m_code, m_desc)
                if mapped and not mapped.get("is_global_fallback"):
                    match_count += 1
            
            if match_count > max_matches:
                max_matches = match_count
                best_match_vendor = candidate

        if best_match_vendor and max_matches > 0:
            try:
                print(f"  [KB] Disambiguated '{extracted_name}' to "
                      f"{best_match_vendor['id']} ({best_match_vendor['name']}) "
                      f"using {max_matches} direct material match(es)")
            except UnicodeEncodeError:
                pass
            return best_match_vendor

    # -- Disambiguation Step 2: City / Postcode (Test MP, no SQLite) --
    if address_hint and len(best_matches) > 1:
        addr_lower = address_hint.lower().strip()
        best_loc_vendor = None
        best_loc_score = -1
        for candidate in best_matches:
            loc = mapper.get_customer_location(candidate["id"])
            loc_score = 0
            # get_customer_location returns {'cities': set, 'postcodes': set}
            for city_val in loc.get("cities", set()):
                if city_val and city_val in addr_lower:
                    loc_score += 1
                    print(f"  [KB] City match: '{city_val}' in address for cust {candidate['id']}")
                    break
            for post_val in loc.get("postcodes", set()):
                if post_val and post_val in addr_lower:
                    loc_score += 2
                    print(f"  [KB] Postcode match: '{post_val}' in address for cust {candidate['id']}")
                    break
            if loc_score > best_loc_score:
                best_loc_score = loc_score
                best_loc_vendor = candidate

        if best_loc_vendor and best_loc_score > 0:
            try:
                print(f"  [KB] City/Postcode disambiguated '{extracted_name}' -> "
                      f"{best_loc_vendor['id']} ({best_loc_vendor['name']}) score={best_loc_score}")
            except UnicodeEncodeError:
                pass
            return best_loc_vendor

    # ── Escalation guard (Step 2 fix from reliability guide) ─────────────────
    # If we still have multiple candidates OR the top score is below the
    # confidence threshold, do NOT auto-pick — return an escalation signal.
    top_candidate = best_matches[0]
    top_score     = top_candidate["score"]

    # Case A: multiple candidates within the ambiguity window after all disambiguation
    still_ambiguous = len(best_matches) > 1 and (
        best_matches[1]["score"] >= top_score - FUZZY_AMBIGUITY_WINDOW
    )
    # Case B: single candidate but score is below confidence threshold
    low_confidence  = top_score < _esc_threshold

    if still_ambiguous or low_confidence:
        reason = "customer_ambiguous" if still_ambiguous else "customer_low_confidence"
        candidate_ids = [m["id"] for m in best_matches]
        try:
            print(
                f"  [KB-ESCALATE] '{extracted_name}' -> {reason} "
                f"(top_score={top_score:.2f}, threshold={_esc_threshold}, "
                f"candidates={candidate_ids}). Routing to human review."
            )
        except UnicodeEncodeError:
            print(f"  [KB-ESCALATE] {reason} top_score={top_score:.2f} candidates={candidate_ids}")
        return {
            "escalation_required":  True,
            "escalation_reason":    reason,
            "top_candidate":        top_candidate,
            "all_candidates":       best_matches,
            "candidate_ids":        candidate_ids,
            "score":                top_score,
            "name":                 top_candidate["name"],
            "id":                   top_candidate["id"],
        }

    # Confident single match above threshold — proceed normally
    return top_candidate


def validate_ship_sold_to(customer_id, extracted_ship, extracted_sold, ship_to_postcode="", raw_text=""):
    """
    Validate Ship-To and Sold-To addresses against Test MP data (no SQLite).

    Reads ship-to rows from SalesOrderMapper.get_ship_to_rows() which is backed
    by the Azure Blob / Test MP parquet.

    Ship-to resolution priority:
      1. Postcode match (extracted from address string) via get_ship_to_info_from_test_mp
      2. City match via get_ship_to_info_from_test_mp
      3. Fuzzy name match (last resort)
    """
    if not customer_id:
        return None

    import re as _re

    # If raw_text contains an Incoterm delivery destination (e.g. 'CIP Freilassing'), include it in extracted_ship
    if raw_text:
        _m_inco = _re.search(r'\b(?:CIP|DAP|DDP|FCA|CPT|FOB|CIF|Lieferort|Delivery\s*to)\s*:?\s*([A-Z\u00c0-\u0178a-z\u00e0-\u00ff]{3,25})\b', raw_text, _re.IGNORECASE)
        if _m_inco and _m_inco.group(0) not in (extracted_ship or ""):
            extracted_ship = f"{extracted_ship or ''} {_m_inco.group(0)}".strip()

    mapper = _get_mapper()
    rows = mapper.get_ship_to_rows(customer_id)
    if not rows:
        return {"warning": f"No master data for customer {customer_id}"}


    # rows = [(sold_to_id, ship_to_id, ship_to_name), ...]
    allowed_sold_tos = set(r[0] for r in rows if r[0])
    ship_to_names    = [(r[1], r[2]) for r in rows if r[1]]  # (id, name) pairs

    result = {"valid_sold_to": False, "valid_ship_to": False}

    if extracted_sold:
        for s in allowed_sold_tos:
            if s and s in extracted_sold:
                result["valid_sold_to"] = True
                result["matched_sold_to_id"] = s
                break

    # -- Ship-to: use postcode/city-aware matching via mapper --
    if extracted_ship:
        # Extract postcode from address string if not already provided
        if not ship_to_postcode:
            pc_match = re.search(r'\b(\d{5,7})\b', extracted_ship)
            ship_postcode = pc_match.group(1) if pc_match else ""
        else:
            ship_postcode = ship_to_postcode

        mp_ship = mapper.get_ship_to_info_from_test_mp(
            customer_id=customer_id,
            ship_to_id="",               # no pre-known ID
            ship_to_address=extracted_ship,
            ship_to_postcode=ship_postcode,
        )
        best_ship_id   = mp_ship.get("ship_to_id",   "")
        best_ship_name = mp_ship.get("ship_to_name", "")

        if best_ship_id:
            result["valid_ship_to"]        = True
            result["matched_ship_to_id"]   = best_ship_id
            result["matched_ship_to_name"] = best_ship_name
        else:
            # Fallback: SequenceMatcher on ship-to names (last resort)
            best_score = 0
            best_id, best_name = None, None
            for s_id, s_name in ship_to_names:
                score = SequenceMatcher(None, extracted_ship.lower(), s_name.lower()).ratio()
                if score > best_score:
                    best_score, best_id, best_name = score, s_id, s_name
            if best_score >= 0.5 and best_id:
                result["valid_ship_to"]        = True
                result["matched_ship_to_id"]   = best_id
                result["matched_ship_to_name"] = best_name
                result["ship_to_match_score"]  = round(best_score, 3)

    if not result["valid_sold_to"] and allowed_sold_tos:
        result["suggestion_sold_to"] = list(allowed_sold_tos)[0]
    if not result["valid_ship_to"] and ship_to_names:
        # Use first named ship-to (deterministic) rather than a random set element
        result["suggestion_ship_to"]      = ship_to_names[0][0]
        result["suggestion_ship_to_name"] = ship_to_names[0][1]

    return result


def _extract_sold_to_from_filename(filename: str) -> str:
    """Extract Sold-to ID from filename if present (e.g. '... Sold-to 4020010339 ...')."""
    m = re.search(r"Sold-to\s*[-_]?\s*(\w+)", filename, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _extract_material_ids_from_filename(filename: str) -> list:
    m = re.search(r"Material[s]?\s+([\d+]+)", filename, re.IGNORECASE)
    if not m:
        return []
    s = m.group(1)
    return [x.strip() for x in re.split(r"\+", s) if x.strip()] if "+" in s else [s]


def reenrich(input_file, output_file, excel_file=None, use_azure_mapper=True):
    """
    Re-enrich a JSONL results file with fresh Celonis data.

    Always uses Azure Blob mapper (SalesOrderMapper.from_azure()) — the
    excel_file argument is kept for CLI backward-compatibility but ignored
    in favour of the Azure source.
    """
    # Always use Azure mapper (fresh Celonis data)
    so_mapper = _get_mapper()
    _load_vendor_cache()  # pre-warm vendor cache from the same mapper instance

    # Audit logger — logs every PO outcome (SO_CREATED / ESCALATED / SKIPPED)
    try:
        from audit_logger import get_audit_logger
        _audit = get_audit_logger()
    except ImportError:
        class _AuditStub:
            def log(self, **_): pass
        _audit = _AuditStub()

    with open(input_file, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    # ── Deduplication: skip duplicate PO source files ─────────────────────────
    # Scenario: CSR sends the same PO email twice → two entries in JSONL with the
    # same source_file.  We enrich only the FIRST occurrence and skip the rest.
    seen_source_files: set = set()
    dedup_records = []
    for r in records:
        sf = (r.get("source_file") or "").strip().lower()
        if not sf or sf in ("", "unknown"):
            # No source_file — keep (can't deduplicate without filename)
            dedup_records.append(r)
            continue
        if sf in seen_source_files:
            po_num = (r.get("header_fields") or {}).get("po_number", "?")
            print(f"  [Dedup] Skipping duplicate source file '{sf}' (PO: {po_num}) — already enriched.")
            continue
        seen_source_files.add(sf)
        dedup_records.append(r)

    if len(dedup_records) < len(records):
        print(f"  [Dedup] {len(records) - len(dedup_records)} duplicate source file(s) removed before enrichment.")
    records = dedup_records

    enriched = []
    for rec in records:
        if "error" in rec and "header_fields" not in rec:
            enriched.append(rec)
            continue


        header = rec.get("header_fields", {})
        v_name = header.get("vendor_name") or header.get("supplier_name")
        src_file = rec.get("source_file", "")
        filename_sold_to = _extract_sold_to_from_filename(src_file) if src_file else None

        # Look up sender_email from AzureEmailTracker if missing in header
        if not header.get("sender_email") and src_file:
            try:
                from azure_email_tracker import AzureEmailTracker
                _trk = AzureEmailTracker()
                _email_ent = _trk.get_email_by_filename(src_file)
                if _email_ent and _email_ent.get("sender_email"):
                    header["sender_email"] = _email_ent["sender_email"]
            except Exception:
                pass

        # Build customer name for fuzzy matching.
        # IMPORTANT: skip non-SAP IDs like "AN01009227077" (Molex vendor codes,
        # buyer ERP IDs etc.) — they don't exist in Test MP sold_to_name_full.
        # A real SAP customer number is all-digits and starts with 4 (e.g. 4020013022).
        raw_customer_id = header.get("customer_id", "")
        is_sap_customer_id = (
            raw_customer_id
            and str(raw_customer_id).strip().isdigit()
            and str(raw_customer_id).strip().startswith("4")
        )
        c_name = (
            filename_sold_to
            or header.get("customer_name")
            or header.get("customer_id_or_name")
            or (raw_customer_id if is_sap_customer_id else None)  # only use if SAP numeric ID
            or header.get("buyer_name")
        )

        # ── Seller Swap Protection ──────────────────────────────────────────
        # On European POs (e.g. Italian 'Messrs', German 'An:'), OCR often extracts
        # ENVALIOR (the seller) as customer_name and the actual buyer as vendor_name.
        # If c_name is ENVALIOR, use v_name / supplier_name as the real customer name.
        if c_name and "ENVALIOR" in str(c_name).upper():
            if v_name and "ENVALIOR" not in str(v_name).upper():
                print(f"  [Seller-Swap] Swapping customer_name '{c_name}' -> real customer '{v_name}' (Envalior is the seller)")
                c_name = v_name

        # Build address hint for SOLD-TO (customer) matching.
        # IMPORTANT: Use BILL TO postcode for customer (sold-to) disambiguation.
        # The sold-to postcode in SAP = the customer's billing/accounts-payable address.
        # ship_to_postcode (delivery address) is passed separately to validate_ship_sold_to.
        bill_to_pc = str(header.get("bill_to_postcode", "") or "").strip()
        ship_to_pc = str(header.get("ship_to_postcode", "") or "").strip()
        addr_parts = [
            header.get("bill_to_address", ""),   # Primary: billing/sold-to address
            header.get("sold_to_address", ""),
            bill_to_pc,                           # Billing postcode for sold-to match
            header.get("country", ""),
            header.get("city", ""),
            header.get("ship_to_address", ""),    # Secondary context (city name)
        ]
        address_hint = " ".join(p for p in addr_parts if p).strip()

        # Fuzzy match vendor (seller-side — usually Envalior, less critical)
        if v_name:
            match = fuzzy_match_vendor(v_name, line_items=rec.get("line_items", []), address_hint=address_hint)
            if match and not match.get("escalation_required"):
                vid = match["id"]
                if vid == "4020036720":
                    vid = "4020010504"
                header["vendor_name_matched"] = match["name"]
                header["vendor_id"] = vid
                header["vendor_name"] = match["name"]
            # Vendor-side escalations are informational only — do not block pipeline

        # Fuzzy match customer  ← THIS is the high-risk match (determines SAP sold-to)
        _customer_escalated = False
        if c_name and c_name != v_name:
            c_match = fuzzy_match_vendor(
                c_name,
                line_items=rec.get("line_items", []),
                address_hint=address_hint,
            )

            # ── ESCALATION GUARD ─────────────────────────────────────────────
            # If fuzzy_match_vendor() returned an escalation signal, do NOT
            # auto-pick a customer. Mark the record and let it fall through
            # to the exception routing in run_outlook_to_pipeline.py (Step 6).
            if c_match and c_match.get("escalation_required"):
                _customer_escalated = True
                _esc_reason  = c_match.get("escalation_reason", "customer_ambiguous")
                _candidates  = c_match.get("candidate_ids", [])
                _top_score   = c_match.get("score", 0.0)
                # Store escalation info in header so exception email can show candidates
                header["customer_match_escalated"]  = True
                header["escalation_reason"]         = _esc_reason
                header["escalation_candidates"]     = _candidates
                header["escalation_top_score"]      = round(_top_score, 3)
                # Leave customer_number / sold_to_id BLANK → pre-flight gate will block push
                rec["sales_orders"] = []
                # Log to audit trail
                _po_num = str(header.get("po_number") or rec.get("source_file") or "").strip()
                _audit.log(
                    po_number=_po_num,
                    source_file=str(rec.get("source_file", "")),
                    customer_name_extracted=str(c_name or ""),
                    matching_method="fuzzy_name",
                    matching_score=_top_score,
                    candidate_customer_ids=_candidates,
                    action="ESCALATED",
                    escalation_reason=_esc_reason,
                    escalation_candidates=_candidates,
                )
                print(
                    f"  [ESCALATE] PO='{_po_num}' customer='{c_name}' escalated "
                    f"({_esc_reason}, score={_top_score:.2f}, candidates={_candidates})"
                )
                enriched.append(rec)
                continue  # skip rest of enrichment for this record
            # ── Material-code fallback ─────────────────────────────────────
            # When the customer's trade name / abbreviation on the PO doesn't
            # fuzzy-match their legal SAP name (e.g. 'JC NORTH AMERICA' vs
            # 'Junchuang North America Inc'), try to identify them uniquely
            # by their customer material code in Test MP.
            if not c_match:
                c_match = _match_customer_by_material(rec.get("line_items", []), customer_name=c_name or "")
                if c_match:
                    print(f"  [KB] Name match failed for '{c_name}' — resolved via material code fallback")
            if c_match:
                customer_id = c_match["id"]
                if customer_id == "4020036720":
                    customer_id = "4020010504"
                header["customer_number"] = customer_id
                header["customer_name_matched"] = c_match["name"]
                header["customer_id_or_name"] = c_match["name"]

                # ── Cross-validate matched customer against PO fields ──────────
                cv = cross_validate_customer_match(
                    sold_to_id       = customer_id,
                    line_items       = rec.get("line_items", []),
                    sales_org        = str(header.get("sales_organization") or header.get("sales_org") or ""),
                    ship_to_postcode = ship_to_pc,   # SHIP TO postcode (delivery location)
                    ship_to_city     = str(header.get("ship_to_address") or "")[:80],
                    sold_to_postcode = bill_to_pc or str(header.get("sold_to_address") or "")[:80],
                )
                header["customer_confidence"] = cv["confidence"]
                header["customer_confidence_explanation"] = cv["explanation"]
                if cv["warnings"]:
                    header["customer_warnings"] = cv["warnings"]

                # Log cross-validation result clearly
                conf_pct = int(cv["confidence"] * 100)
                icon = "[OK]" if cv["confidence"] >= 0.75 else ("[WARN]" if cv["confidence"] >= 0.5 else "[FAIL]")
                expl_safe = cv["explanation"].encode("ascii", errors="replace").decode("ascii")
                print(f"  [CV] {icon} Customer '{c_match['name'].encode('ascii', errors='replace').decode('ascii')}' ({customer_id}) - confidence {conf_pct}%")
                print(f"       {expl_safe}")
                if cv["warnings"]:
                    for w in cv["warnings"]:
                        print(f"       [!] WARNING: {w.encode('ascii', errors='replace').decode('ascii')}")

                # Validate Ship/Sold To
                val_res = validate_ship_sold_to(
                    customer_id,
                    header.get("ship_to_address", ""),
                    header.get("sold_to_address", ""),
                    ship_to_postcode=ship_to_pc,  # SHIP TO postcode (from delivery box, not bill-to)
                    raw_text=rec.get("raw_text", "") or rec.get("email_text", "") or rec.get("email_body", ""),
                )

                if val_res:
                    header["validation"] = val_res
                    if val_res.get("matched_ship_to_id"):
                        header["ship_to_id"] = val_res["matched_ship_to_id"]
                    if val_res.get("matched_ship_to_name"):
                        header["ship_to_name"] = val_res["matched_ship_to_name"]
                    if val_res.get("matched_sold_to_id"):
                        header["sold_to_id"] = val_res["matched_sold_to_id"]
                    elif val_res.get("suggestion_sold_to"):
                        header["sold_to_id"] = val_res["suggestion_sold_to"]
                    if val_res.get("suggestion_ship_to") and not header.get("ship_to_id"):
                        header["ship_to_id"] = val_res["suggestion_ship_to"]
                    if val_res.get("suggestion_ship_to_name") and not header.get("ship_to_name"):
                        header["ship_to_name"] = val_res["suggestion_ship_to_name"]

                if not header.get("sold_to_id") and customer_id:
                    header["sold_to_id"] = customer_id

                # ── WE = AG ENFORCEMENT ─────────────────────────────────────────────
                # If the email/PO says WE=AG (ship-to = sold-to), override whatever
                # ship-to was resolved from address matching. The sold-to party IS
                # the ship-to party in SAP. Example: DUMIS email states "WE = AG"
                # meaning Dumis d.o.o. is both sold-to AND ship-to.
                _raw_email = rec.get("raw_text", "") or rec.get("email_text", "") or rec.get("email_body", "")
                if _detect_we_equals_ag(header, _raw_email):
                    _final_sold_to_id   = header.get("sold_to_id") or customer_id or ""
                    _final_sold_to_name = (header.get("customer_name_matched")
                                           or header.get("customer_name") or "")
                    if _final_sold_to_id:
                        header["ship_to_id"]           = _final_sold_to_id
                        header["ship_to_name"]          = _final_sold_to_name
                        header["ship_to_same_as_sold_to"] = True
                        header["ship_to_source"]        = "WE=AG (ship-to overridden to equal sold-to)"
                        print(
                            f"  [WE=AG] PO='{header.get('po_number','?')}' customer='{_final_sold_to_name}' "
                            f"→ ship_to_id forced = sold_to_id = '{_final_sold_to_id}'"
                        )
                    else:
                        print(
                            f"  [WE=AG-WARN] PO='{header.get('po_number','?')}' WE=AG detected "
                            f"but no sold_to_id resolved yet — ship-to cannot be set."
                        )
                # ─────────────────────────────────────────────────────────────────────────

        # Generate Sales Orders
        email_text = rec.get("email_text", "") or rec.get("email_body", "") or ""
        filename_mats = rec.get("filename_materials")
        if not filename_mats and src_file:
            filename_mats = _extract_material_ids_from_filename(src_file)

        # ── Blanket/Schedule PO filter ────────────────────────────────────────
        # Some customers (e.g. DEP Engineering) send blanket POs where individual
        # release lines carry a status in their 'comment' or 'status' field:
        #   COMPLETE — release already fulfilled (existing SAP SO in Comment field)
        #   FORECAST ONLY — not yet firm; no SO should be created
        # Sending these to SAP would cause duplicate or premature SO creation.
        # We filter them out before mapping; they are preserved in the raw
        # line_items list for audit but excluded from sales_orders generation.
        _SKIP_LINE_STATUS_RE = re.compile(
            r'(?i)\b(?:complete|completed|forecast\s+only|forecast|cancelled|canceled)\b'
        )
        raw_items = rec.get("line_items", [])
        active_items = []
        skipped_items = []
        for li in raw_items:
            # Check both 'status' field and 'comment' field for COMPLETE/FORECAST markers
            status_text = str(li.get("status", "") or "").strip()
            comment_text = str(li.get("comment", "") or "").strip()
            combined = f"{status_text} {comment_text}"
            if _SKIP_LINE_STATUS_RE.search(combined):
                skipped_items.append(li)
            else:
                active_items.append(li)
        if skipped_items:
            print(f"  [BlanketPO] Filtered {len(skipped_items)} COMPLETE/FORECAST line(s) "
                  f"from PO {header.get('po_number', '?')} — only {len(active_items)} active line(s) sent to SAP.")
            rec["_blanket_skipped_lines"] = skipped_items
            # Temporarily replace line_items with active-only for SO generation
            rec["line_items"] = active_items

        rec["sales_orders"] = so_mapper.generate_sales_orders(
            rec,
            filename_materials=filename_mats,
            email_text=email_text,
        )

        # Restore full line_items list (for downstream audit/output)
        if skipped_items:
            rec["line_items"] = raw_items

        # --- Audit log for this record (SO_CREATED or MAPPING_FAILED) ---
        try:
            _has_so = bool(rec.get("sales_orders"))
            _po_audit_num = str(header.get("po_number") or rec.get("source_file") or "").strip()
            _cust_id_audit = str(header.get("customer_number") or header.get("sold_to_id") or "").strip()
            _cust_name_audit = str(header.get("customer_name_matched") or header.get("customer_name") or "").strip()
            _audit.log(
                po_number=_po_audit_num,
                source_file=str(rec.get("source_file", "")),
                customer_name_extracted=str(c_name or ""),
                matching_method="resolved" if _cust_id_audit else "no_match",
                matched_customer_id=_cust_id_audit,
                matched_customer_name=_cust_name_audit,
                extracted_fields=header,
                action="SO_CREATED" if _has_so else "MAPPING_FAILED",
                escalation_reason="" if _has_so else "no_sales_orders_generated",
            )
        except Exception as _ae:
            pass  # audit failure must never block the pipeline

        enriched.append(rec)

    with open(output_file, "w", encoding="utf-8") as f:
        for rec in enriched:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"Done! Saved to {output_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-enrich PO extraction results with fresh Celonis data.")
    parser.add_argument("--input",  default="results_po_examples.jsonl",          help="Input JSONL file")
    parser.add_argument("--output", default="results_po_examples_enriched.jsonl", help="Output JSONL file")
    parser.add_argument("--excel",  default=None,
                        help="(Ignored — kept for backward compatibility) Local Excel file.")
    parser.add_argument("--use-azure-mapper", action="store_true", default=True,
                        help="Always True: pipeline uses Azure Blob / Test MP (not local Excel).")
    args = parser.parse_args()
    reenrich(args.input, args.output)
