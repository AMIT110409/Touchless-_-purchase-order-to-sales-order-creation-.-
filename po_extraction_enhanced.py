import sys
"""
Enhanced PO Extraction Pipeline (v2)
-------------------------------------
Improved version of po_extraction_paddle_langchain.py with:
  1. Tighter LLM prompts (deterministic, no chain-of-thought)
  2. Stronger regex fallbacks (European formats, material codes)
  3. Fuzzy matching via difflib (not just SQL LIKE)
  4. Excel-based Sales Order mapping & generation

Run:
  python po_extraction_enhanced.py --folder test_data
  python po_extraction_enhanced.py --folder test_data --excel EXPORT_20260216_100124.XLSX
"""

import argparse
import json
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import re
import subprocess
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any
from dotenv import load_dotenv

# Load environment variables at the very beginning
load_dotenv()

# Force UTF-8 for console output on Windows to prevent UnicodeEncodeErrors
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.detach())

import fitz  # PyMuPDF for native text extraction

import torch
# SQLAlchemy / SQLite removed — all data now comes from Azure Blob via SalesOrderMapper
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline, BitsAndBytesConfig
from langchain_community.llms import HuggingFacePipeline

from sales_order_mapper import SalesOrderMapper
from claude_client import ClaudeAPIClient, get_claude_client

# ------------------------------------------------

# ------------------------------------------------
# CONFIG
# ------------------------------------------------

MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude").lower() # Default to 'claude'

# -------------------------------------------------------------------
# Azure vs local cache control
# -------------------------------------------------------------------
# FORCE_FRESH_DATA=true  -> always pull latest from Azure Blob (set this on Azure)
# FORCE_FRESH_DATA=false -> use 24h local .celonis_cache/ parquet (local dev default)
FORCE_FRESH_DATA = os.getenv("FORCE_FRESH_DATA", "false").lower() == "true"

# -------------------------------------------------------------------
# Lazy singleton mapper — loaded once per process.
# All vendor/ship-to/material/location data comes from this instance.
# -------------------------------------------------------------------
_mapper_instance = None

def _get_mapper():
    """Return (or create) the shared SalesOrderMapper backed by Azure Blob."""
    global _mapper_instance
    if _mapper_instance is None:
        _mapper_instance = SalesOrderMapper.from_azure(force_refresh=FORCE_FRESH_DATA)
    return _mapper_instance

# ------------------------------------------------
# IMPROVED PROMPT (tighter, direct JSON, no CoT)
# ------------------------------------------------

# ------------------------------------------------
# PROMPT DEFINITIONS
# ------------------------------------------------

SYSTEM_PROMPT = """You are a world-class, multi-lingual document extraction system specializing in Purchase Orders (POs) from any country and language.
Your task is to extract structured data into a precise JSON format, regardless of the document's original language.

### GLOBAL EXTRACTION STRATEGY:
1. **Detect Document Intent**: Identify the PO number, date, and participants based on context, even if labels are in a foreign language.
2. **Key Data Points**:
   - **PO Number**: The unique identifier for this order (e.g., Order No, P.O. #, Reference, Customer PO number).
     * **CRITICAL**: If the document contains a field explicitly labelled **"Customer PO number"**, **"Customer PO"**, **"Klantreferentie"**, **"Ref. commande"**, **"Ihre Bestellnummer"**, or any similar "PO reference" label — ALWAYS extract its value as the PO number, even if the value is alphanumeric text (e.g., "ASOS Order September", "PO-2026-001", "SEPT-ORDER"). Text-based PO numbers are valid — do NOT leave the field empty just because it is not purely numeric.
     * For Italian POs: The PO number is ALWAYS under the label **"ORDINE N."** or **"N. ORDINE"** or **"Numero Ordine"** — typically printed in the **top-right box** of the document. Extract the full value (e.g. "26 055138 A2" from "ORDINE N. 26 055138 A2").
     * **CRITICAL for Italian POs**: The field **"COD. FORN."** (short for "Codice Fornitore") means **SUPPLIER CODE** — it is Envalior's vendor code at the customer (like "221LA112"). It is NOT the PO number. NEVER use a value next to "COD. FORN.", "CODICE FORNITORE", "COD FORNITORE", "FORN.", or "CODICE FORN" as the PO number.
     * NEVER extract a line-item product code (like '56165243') or internal material number as the PO number.
   - **Dates**: Identify 'Order Date' (issue date) and 'Requested Delivery Date' (RDD). Output both as **DD/MM/YYYY**.
   - **Participants**: 
     * **Vendor**: ALWAYS 'Envalior B.V.', 'Envalior', 'Envalior Italy Srl', or 'Envalior SA DE CV' (the seller/recipient).
     * **Customer**: The company placing the order (the buyer). Find their name and ID.
     * **CRITICAL PARTICIPANT RULE**: If the recipient address box (e.g., "Destinatario", "Empfänger", "Deliver to", "Sold to" on the PO) is an entity of the vendor (e.g., "Envalior Italy Srl", "Envalior SA DE CV"), then that entity is the **Vendor** (or seller/recipient), and you must look elsewhere on the document (logos, headers, footers, or issuer boxes) for the actual **Customer** (the buyer placing the order, e.g. "Scherer s.r.l.", "Scherdel", etc.).
3. **Line Items (CRITICAL)**:
   - Extract EVERY line item.
   - `material_code`: Follow this TWO-STEP rule:
     * STEP A — Look for a customer-specific part number or code. **CRITICAL PRIORITY RULES**:
       1. The FIRST code/identifier at the TOP of the "Product / Description" (or "Item / Description") column — appearing BEFORE any descriptive text — is ALWAYS the customer's material code. Use it. Examples of valid customer codes: numeric (0899921583), alphanumeric with dashes (901-01M8-002), short alpha codes (F-X22092), or letter+digit+letter codes like RNY03NAT, RNY01NAT, RCPE06NAT, RCPE06BL, 2CDN580000P0076, or format like '15/1PA6GF30GR' — all are valid customer material codes.
       2. If the description contains a part number format like '15/1PA6GF30GR' or '15/PA6GF30GR', extract that as `material_code`. Do not extract the vendor's internal material number (like '56165243') or any sub-customer number (like 'Stihl Materialnumber: ...') if a customer material code is available.
       3. NEVER use a code that is labelled "Supplier material ref.", "Supplier ref.", "Our material", "DSM grade", "Our part no." or similar — these are the vendor's (Envalior/DSM) internal codes, NOT the customer code. Even if K225-KS, F136-DH, or a similar grade name appears multiple times in the description, it must NOT be used as `material_code` when a separate customer code is present above it in the line item.
       4. If multiple codes appear, always prefer the one LISTED FIRST or in the dedicated product code column over any code mentioned later in the description text.
       5. **ENVALIOR REBRANDING RULE (CRITICAL)**: Some customers (especially Pegasus Polymers) list BOTH the OLD product name (Akulon) AND the NEW product name (Durethan/Stanyl) in the same description, separated by '/' or a space. Example: "Akulon K222-KGV4/BK25019/ Durethan BKV20FN21 90011 BLACK BK25019". In this case:
           - The OLD name starting with **Akulon** (e.g., K222-KGV4/BK25019) is the legacy LANXESS brand — it may NOT be in the current CMIR.
           - The NEW name starting with **Durethan / Stanyl / Pocan / Arnitel** is the current Envalior brand — this IS the one in CMIR.
           - ALWAYS extract the **FULL NEW NAME** (e.g., "BKV20FN21 90011 BLACK BK25019") as `material_code` when both old and new names appear.
           - NEVER use a sub-field labelled "Product id:" or "Product Id:" as the `material_code` — that is the customer's internal batch/product reference, not the CMIR key.
     * STEP B — If NO separate customer part number exists in the line item (the brand/grade name IS the only identifier, e.g. "Akulon F-X22092", "Durethan BKV30H2.0", "Stanyl HGR2", "Pocan B3235"), then you MUST populate `material_code` with that brand/grade name (e.g. "Akulon F-X22092"). Do NOT leave `material_code` empty just because it is a brand name — an empty material_code is ALWAYS wrong when a product name is clearly present.
     * NEVER put quantity, weight, or price values (e.g. "22000 KG", "500", "EUR") in `material_code`.
     * NEVER use a value labelled "Product id:", "Product Id:", "Batch No:", "Batch No." as `material_code` — these are internal batch or product identifiers, not customer material codes.
   - `material_description`: The full text description of the item (including brand/grade names like STANYL, Durethan, Akulon, Pocan). This may duplicate `material_code` when the brand name is the only identifier — that is correct.
   - `quantity`: Extract the numeric quantity only (no unit text in this field).
   - `unit`: The unit of measure exactly as printed on the document. Preserve the original abbreviation verbatim:
     * `T` (bare T), `TON`, `STON` → keep as-is (the system converts US short tons when appropriate)
     * `mt`, `MT`, `mton`, `tonne` → keep as-is (the system converts metric tonnes to KG)
     * `KG`, `KGS`, `KGM` → kilogram
     * `LB`, `LBS` → pound
     * `PCS`, `PC`, `PCE` → piece
     NEVER substitute or guess a unit — copy it exactly from the document.
   - `line_number`: The item sequence number (10, 20, etc.).
   - `packaging`: The packaging type (e.g. Silo, Oktabin, Bulk, Octabin, Bags) mentioned for the line item. Look under the 'Verpack.' or packaging column/text.

### EXTRACTION GUIDANCE FOR ALL LANGUAGES:
- Look for common PO structures: Header (PO#, Date, Customer) → Table (Items, Qty, Price) → Footer (Totals, Addresses).
- Translate/Normalize field values to English or Standard Latin characters where appropriate (keep IDs as-is).
- If multiple codes are present for a material, ALWAYS use the one that appears first / at the top of the line item (that is the customer's code). IGNORE any code that appears in text labelled "Supplier material ref.", "Our reference", "DSM grade", or similar — those are vendor-side identifiers.

### JAPANESE PO GUIDANCE (注文書 / 発注書):
Japanese POs use the following field labels (map them to the same JSON fields as English labels):
- **注文番号 / 発注番号** = PO Number
- **発注日 / 注文日** = Order Date  
- **納入期日 / 納期** = Requested Delivery Date (each line item's delivery date)
- **品名 / 品目コード / 品番** = Material Description / Material Code column
- **数量 / 単位** = Quantity / Unit
- **納入場所 / 納品先** = Delivery Location / Ship-To place name
- **納入先住所** = Ship-To Address
- **仕入先コード** = Supplier Code (Envalior's code at the customer — NOT the PO number)
- **担当者** = Contact Person
- **支払条件** = Payment Terms

**CRITICAL for Japanese material lines:** Japanese POs often show BOTH the customer's own part number AND Envalior's product code (e.g. TW241F10/00001) on the same line item. Envalior product codes start with **TW, PA, BK, SY, SK, TP, AK** followed by digits/letters (e.g. TW241F10/00001, PA6GF30, BKV30H2.0). The **CUSTOMER's material code** is the OTHER code on the same line — typically starting with the customer's own numbering format (e.g. M460100701-R02, K-1234, 0010-ABC). If you see both an Envalior product code (TW.../PA.../BK...) AND a separate customer code on the line item, ALWAYS use the CUSTOMER'S code as `material_code`, not the Envalior code. Also capture the Envalior product code in `material_description`.

### KOREAN PO GUIDANCE (발주서 / 구매 주문서):
- **발주번호 / 주문번호 / 발주 번호** = PO Number (look for `발주번호 :` label in header — e.g. "PO2607000019")
- **발주일 / 주문일 / 발주일자 / 주문일자 / 발행일 / 작성일** = Order Date
- **납기일 / 납기 / 납품기일 / 납기일자 / 배송일** = Requested Delivery Date (each line item's delivery date)
- **품번** = Customer Material Code / Part Number (THIS IS THE CUSTOMER'S OWN PART NUMBER — always use this as `material_code`)
- **품명** = Material Name / Description
- **규격** = Specification (contains the Envalior grade name, e.g. "LANXESS DURETHAN BKV30Q20" — add this to `material_description`)
- **수량 / 발주수량** = Quantity
- **단위** = Unit of measure
- **납품장소 / 납품처 / 배송지 / 납품 주소** = Ship-To delivery location
- **사업장** = Business Site / plant (also a possible ship-to indicator)
- **CRITICAL for Korean dates**: Dates may appear as `YYYY년 MM월 DD일` OR as `YYYY.MM.DD` OR as `YYYY-MM-DD`. Always convert to `DD/MM/YYYY` in your output.
- **CRITICAL — FOOTER DATE**: Some Korean POs print the Order Date ONLY in the **footer** section. Scan entire document for `발주일`, `주문일`, `작성일` labels.
- **CRITICAL — REVERSED COMPANY NAME**: Korean company names often write joint-venture partners in REVERSED word order compared to the English SAP name. For example:
  - Korean PO header: `삼성발레오써멀시스템스주식회사` = "Samsung Valeo Thermal Systems Co."
  - But the SAP/English name is: **"Valeo Samsung Thermal Systems Co., Ltd."** (reversed!)
  - ALWAYS extract the company name exactly as printed, including Korean script. The system will resolve the alias automatically.
- **CRITICAL — 귀중 / 귀하**: These honorific suffixes (meaning "To:" or "Attn:") appear after the Envalior entity name (e.g. "삼성발레오써멀시스템스주식회사 귀중" means "To: Samsung Valeo Thermal Systems"). The word BEFORE 귀중/귀하 is the RECIPIENT (Envalior), and the company at the top-left letterhead is the CUSTOMER (buyer placing the order).


### GERMAN PO GUIDANCE (Bestellung / Kaufvertrag / Lieferabruf):
German POs (e.g. Covestro, Wipak, Südpack, Niederwieser, etc.) use these field labels:
- **Bestellung Nr. / Bestell-Nr. / Bestellnummer** = PO Number (e.g. "2415935002").
- **Unsere Materialnr. / Unsere Material-Nr. / Unsere Materialnummer / Material-Nr. / Art.-Nr. / Artikelnummer** = **CUSTOMER's MATERIAL CODE** (e.g. "00192493"). **ALWAYS extract this value as `material_code`.**
- **Ihre Materialnummer / Ihre Material-Nr. / Ihre Materialnr. / Lieferanten-Art.-Nr.** = **VENDOR'S (Envalior's) Material Code** (e.g. "2790975"). Do NOT use this as `material_code`. Add this to `material_description`.
- **Lieferantennr. / Ihre Lieferantennr. / Kreditor-Nr. / Kunden-Nr.** = Supplier / Vendor Number (Envalior's vendor ID at customer). **NEVER use this as the PO number or material code.**
- **Liefertermin / Wunschtermin / Bestätigter Termin** = Delivery Date. Convert to `DD/MM/YYYY`.

### CRITICAL — WE = AG RULE (Ship-to equals Sold-to):
In German SAP/logistics terminology:
- **WE** (Warenempfänger) = Ship-to party (where goods are physically delivered)
- **AG** (Auftraggeber) = Sold-to party (the customer placing the order)
- When an email or PO states **"WE = AG"**, **"WE=AG"**, **"Lieferadresse = Rechnungsadresse"**, or similar,
  it means the **Ship-to address is identical to the Sold-to address**.
  → Set `ship_to_same_as_sold_to` = true in the output.
  → Do NOT try to extract a separate ship-to address — copy the sold-to / billing address.
  → The ship-to party ID in SAP will be set equal to the sold-to party ID.
  This rule applies even when NO physical delivery address is mentioned in the email body.
  It is COMMON for Slovenian, Austrian, and German customers to use this shorthand.



### ITALIAN PO GUIDANCE (Ordine di Acquisto / Ordine Fornitore):
Italian POs from companies like SCAME MASTAF, Scherer, etc. use these field labels:
- **ORDINE N. / N. ORDINE / Numero Ordine / N. Ordine** = PO Number (usually in the **top-right box**). Extract the FULL value including any letter suffix (e.g. "26 055138 A2" from "ORDINE N. 26 055138 A2").
- **COD. FORN. / CODICE FORNITORE / Cod. Fornitore / Cod Forn** = **SUPPLIER CODE** — this is Envalior's vendor reference number at the customer (e.g. "221LA112"). **NEVER use this as the PO number.**
- **DATA / Data ordine** = Order Date
- **CONSEGNA / Data Consegna** = Requested Delivery Date
- **DESTINAZIONE / Luogo di consegna** = Ship-To delivery location/address
- **CODICE / Codice articolo** = Material Code (customer's part number)
- **DESCRIZIONE** = Material Description
- **QUANTITA' / QTA'** = Quantity
- **U.M.** = Unit of measure
- **CRITICAL**: "Spett. DITTA" (or "Spettabile Ditta") in the address block means "Esteemed Company" and identifies the **recipient** (Envalior). The **customer** (buyer) is identified by the company letterhead/logo and address at the top-left of the document.

### CHINESE PO GUIDANCE (采购订单):
- **采购订单号 / 订单编号** = PO Number
- **物料编号 / 料号** = Material Code
- **数量 / 单位** = Quantity / Unit
- **交货日期** = Delivery Date
- **收货地址 / 送货地址** = Ship-To Address

### CRITICAL ADDRESS RULE — BILL TO vs SHIP TO:
Many POs contain TWO separate address boxes. You MUST treat them differently:
- **BILL TO** (also labelled: Accounts Payable, Invoice To, Billing Address, Rechnungsadresse, 请款地址):
  This is the customer's ACCOUNTING/BILLING address — where invoices are sent.
  Extract this into `bill_to_address` and `bill_to_postcode`.
  The `customer_name` is the company in the BILL TO box.
- **SHIP TO** (also labelled: Deliver To, Delivery Address, Ship To, Lieferadresse, 送货地址, 出货地址):
  This is the PHYSICAL DELIVERY address — where the goods are actually shipped.
  Extract this into `ship_to_address` and `ship_to_postcode`.
  **CRITICAL — CONSIGNEE = SHIP TO**: Some POs (especially Asia-Pacific logistics POs) do NOT have a dedicated "Ship To" box. Instead they use a **Consignee** or **Consignee 1** field to specify the delivery location. If you see labels such as: `Consignee`, `Consignee 1`, `Consignee:`, `Delivery Consignee`, `Notify Party` — treat the address under that label as the SHIP TO delivery address. Extract it into `consignee_address` and `consignee_postcode`. DO NOT leave ship-to empty just because the label says "Consignee" instead of "Ship To".
  **CRITICAL — ITALIAN / EUROPEAN PO DOCUMENT NUMBER (`Numero documento` / `Ordine N.`)**:
  - In Italian and European purchase orders (labeled `Ordine`, `sistema S.r.l.`, `N° Documento`, `Numero documento`, `Order`), the Purchase Order number is located under `Numero documento`, `N° Documento`, `Numero Ordine`, or `Ordine N.` (e.g. `97`, `105`, `42`).
  - **NEVER** extract bank names (such as `Vs. banca`, `DEUTSCHE`, `DEUTSCHE BANK`, `BNP`, `UNICREDIT`, `INTESA`, `COMMERZBANK`, `BONIFICO`) or payment terms as `po_number`. `Vs. banca` is the customer's bank name, NOT the purchase order number!
  - Short document numbers (e.g. `97`, `42`, `101`) are valid PO numbers and MUST be extracted into `po_number`.

  **CRITICAL — SELLER / VENDOR ADDRESS IS NOT SHIP-TO**:
  - Purchase Orders and Contracts list ENVALIOR (e.g. `ENVALIOR INDIA PRIVATE LIMITED`, `ENVALIOR SINGAPORE PTE LTD`, `ENVALIOR DEUTSCHLAND GMBH`, `DSM`, `LANXESS`) as the **SELLER** / Vendor.
  - **NEVER** extract Envalior's seller address (e.g. `PLOT NO. F-40 MIDC RANJANGAON PUNE`) as `ship_to_address` or `bill_to_address`. Envalior is the SELLER, not the customer!
  - `customer_name` and `bill_to_address` is the company placing the order (e.g. `INABATA SINGAPORE (PTE.) LTD.`).
  - `ship_to_address` / `consignee_address` is the destination where goods are delivered (e.g. `INABATA VIETNAM CO., LTD ROOM 902B HANOI VIETNAM` or `Delivery to:` location).

  **CRITICAL — PURCHASE CONTRACTS & ORDER NUMBERS (`Contract No.`)**:
  - On Purchase Contracts (such as Inabata, Molex, Foxconn), the PO Number is labeled **`Contract No.`** (e.g. `CSG0152081`), **`Seller's Order No.`**, or **`Order No.`**. Extract `CSG0152081` as `po_number`. NEVER extract an email date/timestamp like `20260714100802` as `po_number`.

NEVER copy the BILL TO postcode into `ship_to_postcode`. They are different locations and different postcodes.
If the PO has only ONE address box, use it for both.

### CRITICAL — MULTIPLE SHIP-TO PARTIES PER PO:
Some POs (especially Taiwan/Asia-Pacific) include a **'ship to'** or **'deliver to'** column inside the **line-item table**, where DIFFERENT rows ship to DIFFERENT locations/companies. When you see this:
- Populate `line_ship_to_name`, `line_ship_to_address`, and `line_ship_to_postcode` on EACH line item with the value shown in that column for that row.
- The **header-level** `ship_to_address` should still contain the primary / first ship-to address.
- Example: Row 1 ships to '訊知薇宏倉 (高雄市)' and Row 2 ships to '湘潤 (高雄市)' → each line gets its own `line_ship_to_name`.
- If ALL lines share the same ship-to, leave `line_ship_to_name` / `line_ship_to_address` / `line_ship_to_postcode` as empty strings.

### Output Schema:
{
  "header_fields": {
    "po_number": "string",
    "order_date": "string (DD/MM/YYYY)",
    "requested_delivery_date": "string (DD/MM/YYYY)",
    "customer_name": "string (from BILL TO box — the company placing the order)",
    "customer_id": "string",
    "bill_to_address": "string (full BILL TO / billing / accounts payable address)",
    "bill_to_postcode": "string (postcode from BILL TO address ONLY — NOT from Ship To)",
    "ship_to_address": "string (full SHIP TO / delivery address — where goods are physically delivered)",
    "ship_to_postcode": "string (postcode from SHIP TO delivery address ONLY — NOT from Bill To)",
    "sold_to_address": "string",
    "ship_to_same_as_sold_to": "boolean — set TRUE when the document/email explicitly states WE=AG, WE = AG, ship-to equals sold-to, or Lieferadresse = Rechnungsadresse. Leave false otherwise.",
    "consignee_address": "string (full Consignee 1 / Consignee delivery address — only populate when the PO uses Consignee field instead of Ship To)",
    "consignee_postcode": "string (postcode from Consignee address — only when PO uses Consignee field)"
  },
  "line_items": [
    {
      "line_number": "string",
      "material_code": "string",
      "material_description": "string",
      "quantity": "string",
      "unit": "string",
      "unit_price": "string",
      "delivery_date": "string (DD/MM/YYYY)",
      "packaging": "string (packaging type, e.g. Oktabin, Silo, Bulk, etc. if mentioned on the line item)",
      "line_ship_to_name": "string (delivery company / location name for THIS specific line item — ONLY populate when the PO line-item table has a 'ship to' or 'deliver to' column with DIFFERENT values per row; leave empty string when all lines share the same ship-to)",
      "line_ship_to_address": "string (delivery address for THIS specific line item — ONLY when different per line)",
      "line_ship_to_postcode": "string (postcode of THIS line item's delivery address — ONLY when different per line)"
    }
  ],
  "reasoning": "Language detected and summary of why certain fields were mapped."
}

**IMPORTANT**: RETURN ONLY RAW JSON. No markdown blocks, no conversational preamble."""

PROMPT_TEMPLATE_LOCAL = f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n\n<|im_start|>user\nDOCUMENT TEXT:\n{{content}}<|im_end|>\n<|im_start|>assistant\n"

PROMPT_TEMPLATE_CLAUDE = "DOCUMENT TEXT:\n{content}\n\nExtract the data according to the instructions and output ONLY the JSON object."

# ------------------------------------------------
# Knowledge Base - Fuzzy Matching (difflib)
# ------------------------------------------------


# Cache vendor names to avoid repeated DB calls
_vendor_cache = None

def _load_vendor_cache():
    """
    Load all vendor (sold_to_id, sold_to_name_full) pairs from Test MP parquet.
    No SQLite — data comes from Azure Blob via SalesOrderMapper.
    Deduplicates doubled names from Celonis Test MP storage format
    (e.g. 'JUNCHUANG NORTH AMERICA JUNCHUANG NORTH AMERICA' → 'JUNCHUANG NORTH AMERICA').
    """
    global _vendor_cache
    if _vendor_cache is not None:
        return _vendor_cache
    raw = _get_mapper().get_vendor_list()
    deduped = []
    for vid, vname in raw:
        parts = vname.strip().split()
        half  = len(parts) // 2
        if half >= 2 and parts[:half] == parts[half:]:
            vname = " ".join(parts[:half])
        deduped.append((vid, vname))
    _vendor_cache = deduped
    return _vendor_cache


def _extract_material_ids_from_filename(filename: str) -> list:
    """
    Extract material IDs from filename if present (e.g. '... Material 15219+48029.pdf').
    Returns a list of strings.
    """
    m = re.search(r"Material[s]?\s+([\d+]+)", filename, re.IGNORECASE)
    if not m:
        return []
    s = m.group(1)
    if "+" in s:
        return [x.strip() for x in re.split(r"\+", s) if x.strip()]
    else:
        return [s.strip()]


def _extract_sold_to_from_filename(filename: str) -> str:
    """
    Extract sold-to ID from filename if present (e.g. '... Sold-to 4020010339 ...').
    """
    m = re.search(r"Sold-to\s*[-_]?\s*(\w+)", filename, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def extract_eml_file(eml_path: Path, output_dir: Path):
    """Extract body and attachments from a .eml file using standard python email library."""
    try:
        import email
        import email.policy
        with open(eml_path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=email.policy.default)
        
        # Save body
        body = msg.get_body(preferencelist=('plain', 'html'))
        if body:
            content = body.get_content()
            if content:
                body_filename = f"{eml_path.stem}_body.txt"
                (output_dir / body_filename).write_text(str(content), encoding="utf-8", errors="replace")
                print(f"  [EML] Extracted body to {body_filename}")

        # Save attachments
        for part in msg.walk():
            filename = part.get_filename()
            if filename:
                clean_name = re.sub(r'[<>:"/\\|?*]', "_", filename)
                payload = part.get_payload(decode=True)
                if payload:
                    att_path = output_dir / f"{eml_path.stem}_att_{clean_name}"
                    att_path.write_bytes(payload)
                    print(f"  [EML] Extracted attachment: {clean_name}")
    except Exception as e:
        print(f"  [EML] Error extracting {eml_path}: {e}")


def extract_msg_file(msg_path: Path, output_dir: Path):
    """Extract body and attachments from a .msg file using extract-msg."""
    try:
        import extract_msg
        with extract_msg.openMsg(str(msg_path)) as msg:
            # Extract body
            body = msg.body
            if body:
                body_filename = f"{msg_path.stem}_body.txt"
                body_path = output_dir / body_filename
                body_path.write_text(body, encoding="utf-8", errors="replace")
                print(f"  [MSG] Extracted body to {body_filename}")

            # Extract attachments
            if msg.attachments:
                for att in msg.attachments:
                    att_name = att.longFilename or att.filename
                    if not att_name:
                        continue
                    # Clean filename characters just in case
                    att_name = re.sub(r'[<>:"/\\|?*]', "_", att_name)
                    att_path = output_dir / f"{msg_path.stem}_att_{att_name}"
                    try:
                        if hasattr(att, 'data') and att.data:
                            att_path.write_bytes(att.data)
                        else:
                            att.save(customPath=str(att_path))
                        print(f"  [MSG] Extracted attachment: {att_name}")
                    except Exception as _e:
                        print(f"  [MSG] Error saving attachment {att_name}: {_e}")
    except Exception as e:
        print(f"  [MSG] Error extracting {msg_path}: {e}")


def preprocess_msg_files(folder_path: Path):
    """Recursively find and extract .msg and .eml email files."""
    processed = set()
    has_new = True
    max_depth = 5
    depth = 0
    
    while has_new and depth < max_depth:
        has_new = False
        depth += 1
        email_files = list(folder_path.rglob("*.msg")) + list(folder_path.rglob("*.eml"))
        for email_path in email_files:
            if email_path.resolve() in processed:
                continue
            
            name_lower = email_path.name.lower()
            _skip_prefixes = ('[po exception', '[robona-attach]', 'undeliverable')
            _skip_substrings = ('ai processing failed', 'po exception -')
            if any(name_lower.startswith(pfx) for pfx in _skip_prefixes) or \
               any(sub in name_lower for sub in _skip_substrings):
                print(f"  [EMAIL] Skipping system exception email (not a PO): {email_path.name[:80]}")
                processed.add(email_path.resolve())  # mark as seen so we don't re-check
                continue

            print(f"  [EMAIL] Pre-processing email file: {email_path.name}")
            if email_path.suffix.lower() == ".eml":
                extract_eml_file(email_path, email_path.parent)
            else:
                extract_msg_file(email_path, email_path.parent)
            processed.add(email_path.resolve())
            has_new = True



# Customer name aliases: extracted name -> KB name for fuzzy lookup
CUSTOMER_ALIASES = {
    # Niederwieser GmbH (SAP 4020000825, Kreditoren Nr. K00162)
    # The ship-to/warehouse is 'Hauptlager - VF Verpackungen GmbH' (same company, same SAP ID)
    # All variants of both names must resolve to 'Niederwieser GmbH' which IS in the KB.
    "niederwieser": "Niederwieser GmbH",
    "hauptlager - niederwieser gmbh": "Niederwieser GmbH",
    "hauptlager - niederwieser": "Niederwieser GmbH",
    "vf verpackungen": "Niederwieser GmbH",         # VF Verpackungen = Niederwieser warehouse
    "hauptlager - vf verpackungen": "Niederwieser GmbH",  # Fixed: was 'VF Verpackungen GmbH' (not in KB)
    "hauptlager vf verpackungen": "Niederwieser GmbH",
    "bando chemical industries": "4020013043",   # Bando Kagaku (Japan), SAP sold-to 4020013043
    "bando chemical": "4020013043",
    "bando kagaku kabushiki kaisha": "4020013043",
    "bando kagaku co": "4020013043",
    "bando kagaku": "4020013043",
    # Japanese script aliases — LLM may return the company name in kanji/katakana
    "\u30d0\u30f3\u30c9\u30fc\u5316\u5b66\u682a\u5f0f\u4f1a\u793e": "4020013043",   # バンドー化学株式会社
    "\u30d0\u30f3\u30c9\u30fc\u5316\u5b66": "4020013043",                            # バンドー化学 (short form)
    "\u30b0\u30ed\u30fc\u30d0\u30eb\u30dd\u30ea\u30a2\u30bb\u30bf\u30fc\u30eb\u682a\u5f0f\u4f1a\u793e": "4020020391",  # グローバルポリアセタール株式会社
    "\u30b0\u30ed\u30fc\u30d0\u30eb\u30dd\u30ea\u30a2\u30bb\u30bf\u30fc\u30eb": "4020020391",              # グローバルポリアセタール
    "global polyacetal": "4020020391",
    "global polyacetal co": "4020020391",
    "gpac": "4020020391",

    # Korean aliases (if any Korean POs arrive)
    # NOTE: Korean company names often reverse the word order compared to English SAP names.
    # Example: "삼성발레오써멀시스템스주식회사" = Samsung Valeo Thermal Systems
    #          but SAP name is "Valeo Samsung Thermal Systems Co., Ltd." (reversed!)
    # Customer ID: 4020038183, Sales Org: 2760, CSR: CLARE.JIN@ENVALIOR.COM
    "삼성발레오써멀시스템스주식회사": "4020038183",
    "삼성발레오써멀시스템스": "4020038183",
    "삼성발레오쓰몀시스템스주식회사": "4020038183",
    "삼성발레오쓰몀시스템스": "4020038183",
    "samsung valeo thermal systems": "4020038183",    # romanized (reversed Korean order)
    "samsung valeo thermal": "4020038183",
    "valeo samsung thermal systems": "4020038183",    # SAP name order
    "valeo samsung thermal": "4020038183",


    "ecoform multifol": "Ecoform Multifol Verpackungsfolien",
    "mf-folien": "MF Folien GmbH",
    "mf folien": "MF Folien GmbH",
    "molex interconnect": "上海莫仕连接器有限公司",
    "molex interconnect (shanghai)": "上海莫仕连接器有限公司",
    "molex interconnect (shanghai) co": "上海莫仕连接器有限公司",
    "molex japan": "NIHON MOLEX",
    "molex japan llc": "NIHON MOLEX",
    "ergotech srl a socio unico": "Ergotech SRL",
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
    # Scherdel Wiesauplast de México (San José Iturbide, Guanajuato, MX) — SalesOrg 2600
    # The Celonis sold_to_name_full is a 170-char doubled name+address string that scores
    # ~0.30 in SequenceMatcher, so we pin it to the customer ID directly.
    "scherdel wiesauplast de m xico": "4020010062",   # é→space after regex cleaning
    "scherdel wiesauplast de mexico": "4020010062",   # POs without accent
    "scherdel wiesauplast mexico": "4020010062",
    # Scherdel Wiesauplast Deutschland GmbH & Co. KG — SalesOrg 2500/2540
    "scherdel wiesauplast deutschland": "4020025252",
    # Scherer s.r.l. (Italy, postcode 39040) — SalesOrg 2545
    "scherer s r l": "4020044050",     # after [^a-z0-9\s] cleaning: '.' removed
    "scherer srl": "4020044050",
}


def fuzzy_match_vendor(extracted_name: str, threshold: float = 0.65, line_items: list = None, address_hint: str = "") -> dict:
    """
    Fuzzy match an extracted vendor/customer name against the knowledge base.
    Uses difflib.SequenceMatcher for scoring.
    Optionally checks line_items against database materials to disambiguate multiple matches.
    Returns: {"name": ..., "id": ..., "score": ...} or None
    """
    if not extracted_name or len(extracted_name) < 3:
        return None

    # Apply customer aliases (e.g. Niederwieser -> VF Verpackungen GmbH)
    lookup_name = extracted_name
    ext_lower = extracted_name.lower().strip()
    # ASCII-cleaned version for most aliases (strips punctuation but keeps a-z, 0-9)
    ext_clean = re.sub(r'[^a-z0-9\s]', ' ', ext_lower)
    ext_clean = ' '.join(ext_clean.split())
    # Raw Unicode version for Japanese/Korean/Chinese aliases (do not strip CJK chars)
    ext_unicode_clean = re.sub(r'[^\w\s]', ' ', extracted_name, flags=re.UNICODE).strip().lower()

    for alias, canonical in CUSTOMER_ALIASES.items():
        alias_lower = alias.lower()
        # Try 1: ASCII-cleaned match (works for European/Latin languages)
        alias_clean = re.sub(r'[^a-z0-9\s]', ' ', alias_lower)
        alias_clean = ' '.join(alias_clean.split())
        # Try 2: Raw Unicode substring match (works for Japanese/Korean/Chinese)
        alias_unicode = re.sub(r'[^\w\s]', ' ', alias, flags=re.UNICODE).strip().lower()

        matched = (alias_clean and alias_clean in ext_clean) or \
                  (alias_unicode and alias_unicode in ext_unicode_clean)

        if matched:
            if str(canonical).isdigit():
                vendors = _load_vendor_cache()
                if vendors:
                    for vendor_id, vendor_name in vendors:
                        if vendor_id == canonical:
                            return {"name": vendor_name, "id": vendor_id, "score": 1.0}
            lookup_name = canonical
            break

    vendors = _load_vendor_cache()
    if not vendors:
        return None

    extracted_lower = lookup_name.lower().strip()
    
    # -- Exact ID Match --
    if sum(c.isdigit() for c in extracted_lower) > len(extracted_lower) / 2:
        clean_extracted_id = "".join(filter(str.isdigit, extracted_lower))
        for vendor_id, vendor_name in vendors:
            if vendor_id and vendor_id.endswith(clean_extracted_id):
                return {"name": vendor_name, "id": vendor_id, "score": 1.0}

    # -- Fuzzy Name Match --
    matches = []
    for vendor_id, vendor_name in vendors:
        v_lower = vendor_name.lower()
        # Quick pre-filter
        alpha_prefix = ''.join(c for c in extracted_lower if c.isalnum())[:2]
        v_alpha = ''.join(c for c in v_lower if c.isalnum())
        if len(alpha_prefix) >= 2 and alpha_prefix not in v_alpha:
            continue

        score = SequenceMatcher(None, extracted_lower, v_lower).ratio()
        if score >= threshold:
            matches.append({"name": vendor_name, "id": vendor_id, "score": round(score, 3)})

    if not matches:
        return None

    # Sort matches by score descending
    matches.sort(key=lambda x: x["score"], reverse=True)
    best_score = matches[0]["score"]
    
    # Get all matches that share the best score (or within 0.08 margin)
    best_matches = [m for m in matches if m["score"] >= best_score - 0.08]

    if len(best_matches) == 1:
        return best_matches[0]

    # Disambiguate using line items
    if line_items:
        po_materials = set()
        for item in line_items:
            m_code = item.get("material_code")
            if m_code:
                po_materials.add(str(m_code).strip())

        if po_materials:
            best_match_vendor = None
            max_matches = -1
            # Use Test MP data via mapper
            mapper = _get_mapper()
            for candidate in best_matches:
                db_mats = mapper.get_materials_for_customer(candidate["id"])
                match_count = sum(1 for m in po_materials if m.lower() in db_mats)
                if match_count > max_matches:
                    max_matches = match_count
                    best_match_vendor = candidate

            if best_match_vendor and max_matches > 0:
                try:
                    print(f"  [KB] Disambiguated '{extracted_name}' to {best_match_vendor['id']} ({best_match_vendor['name']}) using {max_matches} material match(es)")
                except UnicodeEncodeError:
                    safe_name = str(best_match_vendor['name']).encode('ascii', 'ignore').decode('ascii')
                    safe_ext = str(extracted_name).encode('ascii', 'ignore').decode('ascii')
                    print(f"  [KB] Disambiguated '{safe_ext}' to {best_match_vendor['id']} ({safe_name}) using {max_matches} material match(es)")
                return best_match_vendor

    # -- Disambiguation Step 2: City / Postcode (Test MP, no SQLite) --
    if address_hint and len(best_matches) > 1:
        addr_lower = address_hint.lower().strip()
        mapper = _get_mapper()
        best_loc_vendor = None
        best_loc_score  = -1
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
                best_loc_score  = loc_score
                best_loc_vendor = candidate

        if best_loc_vendor and best_loc_score > 0:
            try:
                print(f"  [KB] City/Postcode disambiguated '{extracted_name}' -> {best_loc_vendor['id']} ({best_loc_vendor['name']}) score={best_loc_score}")
            except UnicodeEncodeError:
                pass
            return best_loc_vendor

    return best_matches[0]


def validate_ship_sold_to(
    vendor_id: str,
    extracted_ship: str,
    extracted_sold: str,
    ship_to_postcode: str = "",
    raw_text: str = "",
) -> dict:
    """
    Validates Ship-To and Sold-To addresses against Test MP data (no SQLite).
    Reads ship-to rows from SalesOrderMapper.get_ship_to_rows() backed by Azure Blob.

    Ship-to resolution priority:
      1. Postcode match (ship_to_postcode against Test MP ship_to_postcode column)
      2. Exact ID containment in extracted_ship string
      3. Fuzzy name match (last resort)
    """
    if not vendor_id:
        return None

    import re as _re

    # If raw_text contains an Incoterm delivery destination (e.g. 'CIP Freilassing'), include it in extracted_ship
    if raw_text:
        _m_inco = _re.search(r'\b(?:CIP|DAP|DDP|FCA|CPT|FOB|CIF|Lieferort|Delivery\s*to)\s*:?\s*([A-Z\u00c0-\u0178a-z\u00e0-\u00ff]{3,25})\b', raw_text, _re.IGNORECASE)
        if _m_inco and _m_inco.group(0) not in (extracted_ship or ""):
            extracted_ship = f"{extracted_ship or ''} {_m_inco.group(0)}".strip()

    rows = _get_mapper().get_ship_to_rows(vendor_id)
    if not rows:
        return {"warning": f"No master data for vendor {vendor_id}"}

    # rows = [(sold_to_id, ship_to_id, ship_to_name), ...]
    allowed_sold_tos = set(r[0] for r in rows if r[0])
    allowed_ship_tos = set(r[1] for r in rows if r[1])
    ship_to_names    = [(r[1], r[2]) for r in rows if r[2]]  # (id, name) pairs

    result = {
        "valid_sold_to": False,
        "valid_ship_to": False,
    }

    # Validate Sold-To (exact ID containment)
    if extracted_sold:
        for s in allowed_sold_tos:
            if s and s in extracted_sold:
                result["valid_sold_to"] = True
                result["matched_sold_to_id"] = s
                break

    # -- Ship-to resolution: postcode first, then ID, then fuzzy name --
    # Step 1: Postcode match (most reliable for cases like 'TX76140')
    if (ship_to_postcode or extracted_ship) and not result.get("valid_ship_to"):
        clean_pc = _re.sub(r"\D", "", ship_to_postcode.strip()) if ship_to_postcode else ""

        if clean_pc:
            try:
                # Also try to extract city from the ship-to address text for tie-breaking
                _city_hint = ""
                if extracted_ship:
                    _cm = _re.search(
                        r'\b' + _re.escape(clean_pc) + r'\b[\s,]+([A-Z\u00c0-\u0178a-z\u00e0-\u00ff][A-Z\u00c0-\u0178a-z\u00e0-\u00ff\s-]{1,30}?)(?:\s*[\(,]|$)',
                        extracted_ship, _re.IGNORECASE
                    )
                    if _cm:
                        _city_hint = _cm.group(1).strip()

                mp_ship = _get_mapper().get_ship_to_info_from_test_mp(
                    customer_id=vendor_id,
                    ship_to_id="",
                    ship_to_address=extracted_ship or "",
                    ship_to_postcode=clean_pc,
                    ship_to_city=_city_hint,
                )
                best_id   = mp_ship.get("ship_to_id", "")
                best_name = mp_ship.get("ship_to_name", "")
                if best_id:
                    result["valid_ship_to"]        = True
                    result["matched_ship_to_id"]   = best_id
                    result["matched_ship_to_name"] = best_name
            except Exception:
                pass  # fall through to ID/fuzzy matching


    # Step 2: Exact ID containment in address string
    if extracted_ship and not result.get("valid_ship_to"):
        for s_id in allowed_ship_tos:
            if s_id and s_id in extracted_ship:
                result["valid_ship_to"] = True
                result["matched_ship_to_id"] = s_id
                break

    # Step 3: Fuzzy name match (last resort)
    if extracted_ship and not result.get("valid_ship_to") and ship_to_names:
        best_score = 0
        best_id = None
        best_name = None
        for s_id, s_name in ship_to_names:
            score = SequenceMatcher(None, extracted_ship.lower(), s_name.lower()).ratio()
            if score > best_score:
                best_score = score
                best_id = s_id
                best_name = s_name
        if best_score >= 0.5 and best_id:
            result["valid_ship_to"] = True
            result["matched_ship_to_id"] = best_id
            result["matched_ship_to_name"] = best_name
            result["ship_to_match_score"] = round(best_score, 3)

    # Suggestions if nothing matched
    if not result["valid_sold_to"] and allowed_sold_tos:
        result["suggestion_sold_to"] = list(allowed_sold_tos)[0]
    if not result["valid_ship_to"] and allowed_ship_tos:
        result["suggestion_ship_to"] = list(allowed_ship_tos)[0]

        if ship_to_names:
            result["suggestion_ship_to_name"] = ship_to_names[0][1]

    return result


# ------------------------------------------------
# LLM Setup (deterministic, no sampling)
# ------------------------------------------------
llm_pipeline = None
claude_client = None


def setup_llm():
    global llm_pipeline, claude_client
    
    if LLM_PROVIDER == "claude":
        print("Using Claude API Provider...")
        try:
            claude_client = get_claude_client()
            print("Claude client initialized.")
        except Exception as e:
            print(f"Error initializing Claude: {e}")
            sys.exit(1)
        return

    print(f"Loading local model: {MODEL_ID} on {DEVICE} ...")
    try:
        # Re-enable Quantization for 4GB GPUs
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

        load_kwargs = {"device_map": "auto"}

        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            quantization_config=quantization_config,
            **load_kwargs
        )

        pipe = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=4096,
            temperature=0.0,       # Deterministic
            do_sample=False,       # No sampling = consistent output
            return_full_text=False
        )
        llm_pipeline = HuggingFacePipeline(pipeline=pipe)
        print("Local LLM loaded successfully.")

    except Exception as e:
        print(f"Error loading local LLM: {e}")
        sys.exit(1)


# ------------------------------------------------
# JSON Extraction (robust brace matching)
# ------------------------------------------------

def _extract_json_from_text(text_str: str) -> dict:
    """Extract the first valid JSON object from LLM output.
    Uses multi-layer repair: direct parse -> json_repair -> manual fixes.
    """
    if not isinstance(text_str, str):
        return {}

    cleaned = text_str.strip()
    cleaned = re.sub(r"```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*", "", cleaned)

    # Find the first balanced JSON object
    start = cleaned.find("{")
    if start == -1:
        return {"error": "No JSON found", "raw": text_str[:200]}

    balance = 0
    end = -1
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            balance += 1
        elif cleaned[i] == "}":
            balance -= 1
            if balance == 0:
                end = i
                break

    if end == -1:
        snippet = cleaned[start:]
    else:
        snippet = cleaned[start:end + 1]

    # -- Attempt 1: Direct parse --
    try:
        obj = json.loads(snippet)
        if "data" in obj and isinstance(obj["data"], dict):
            return obj["data"]
        return obj
    except json.JSONDecodeError:
        pass

    # -- Attempt 2: json_repair library --
    try:
        from json_repair import repair_json
        repaired = repair_json(snippet, return_objects=True)
        if isinstance(repaired, dict):
            print("  [JSON] Repaired via json_repair ✓")
            if "data" in repaired and isinstance(repaired["data"], dict):
                return repaired["data"]
            return repaired
    except ImportError:
        pass
    except Exception:
        pass

    # -- Attempt 3: Manual fixes for common LLM errors --
    try:
        fixed = snippet
        fixed = re.sub(r",\s*}", "}", fixed)     # trailing comma before }
        fixed = re.sub(r",\s*]", "]", fixed)      # trailing comma before ]
        fixed = fixed.replace("'", '"')            # single → double quotes
        obj = json.loads(fixed)
        print("  [JSON] Repaired via manual fixes ✓")
        if "data" in obj and isinstance(obj["data"], dict):
            return obj["data"]
        return obj
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}", "raw": snippet[:200]}


# ------------------------------------------------
# Post-processing & Regex Fallbacks
# ------------------------------------------------

# CJK unknown-value markers that should be treated as null
_UNKNOWN_MARKERS = {"不明", "未提供", "unknown", "N/A", "n/a", "NA", "なし", "无", "未知", "不詳"}

def _clean_unknown_values(obj):
    """Recursively replace CJK/EN 'unknown' placeholders with None."""
    if isinstance(obj, dict):
        return {k: _clean_unknown_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean_unknown_values(item) for item in obj]
    elif isinstance(obj, str) and obj.strip() in _UNKNOWN_MARKERS:
        return None
    return obj

def _is_weight_as_material_code(value: str) -> bool:
    """Detect if material_code was wrongly filled with quantity/weight/price (e.g. '25Kg', '500 KG', '70620 EUR')."""
    if not value or not isinstance(value, str):
        return False
    s = value.strip()
    # Pattern: "25Kg", "500 KG", "1.500 kg", "1000 pcs" - looks like qty+unit, not material code
    if re.match(r"^\d+([.,]\d+)?\s*(kg|KG|pcs|pc|ton|lb|g|eur|€)\s*$", s, re.IGNORECASE):
        return True
    if re.match(r"^\d+\s*(kg|KG|pcs|ton)\s*$", s, re.IGNORECASE):
        return True
    # Pattern: "70620 EUR", "15450 EUR" - price/amount wrongly in material_code
    if re.match(r"^\d+([.,]\d+)?\s*EUR\s*$", s, re.IGNORECASE):
        return True
    return False


def _extract_sold_to_from_filename(filename: str) -> str | None:
    """Extract Sold-to customer ID from filename. E.g. 'Sold-to 4020036720 - Material 15219' -> 4020036720."""
    m = re.search(r"Sold-to\s+(\d{8,12})", filename, re.IGNORECASE)
    return m.group(1) if m else None


def _extract_material_ids_from_filename(filename: str) -> list:
    """
    Extract internal material IDs from filename.
    Examples: 'Material 14885' -> ['14885'], 'Materials 16552+47206+61262+61457' -> ['16552','47206','61262','61457']
    """
    m = re.search(r"Material[s]?\s+([\d+]+)", filename, re.IGNORECASE)
    if not m:
        return []
    s = m.group(1)
    if "+" in s:
        return [x.strip() for x in re.split(r"\+", s) if x.strip()]
    return [s]


def _normalize_european_number(value: Any) -> str:
    """
    Convert European number formats to standard.
    '1.500,00' -> '1500.00'
    '1.500' (if context says qty) -> '1500'
    '24,000' -> '24000'
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    
    val = value.strip()

    # Pattern: 1.500,00 (dot=thousands, comma=decimal)
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", val):
        val = val.replace(".", "").replace(",", ".")
        # Remove trailing .00
        if val.endswith(".00"):
            val = val[:-3]
        return val

    # Pattern: 1,500.00 (comma=thousands, dot=decimal)
    if re.match(r"^\d{1,3}(,\d{3})+(\.\d+)?$", val):
        val = val.replace(",", "")
        if val.endswith(".00"):
            val = val[:-3]
        return val

    # Pattern: 24,000 (comma as thousands)
    if re.match(r"^\d{1,3},\d{3}$", val):
        return val.replace(",", "")

    return val


def _clean_quantity(qty_str: Any) -> str:
    """Strip units and normalize a quantity string."""
    if qty_str is None:
        return ""
    if not isinstance(qty_str, str):
        qty_str = str(qty_str)
        
    # Remove unit suffixes (kg, pcs, m, ea, bag, ton, lbs, stück/stuck)
    cleaned = re.sub(r"\s*(kg|pcs|m|ea|bag|ton|lbs?|st[üu]ck)\s*$", "", qty_str, flags=re.IGNORECASE)
    cleaned = _normalize_european_number(cleaned.strip())
    # Remove any remaining non-numeric chars except dot
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    return cleaned


_MONTH_NAMES_MAP = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'june': 6, 'jun': 6,
    'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12
}

def _normalize_date(date_str: str, is_us_format: bool = False) -> str:
    """
    Normalize any raw date string to strict DD/MM/YYYY format for Celonis & SAP.

    Handles:
    - MM/DD/YYYY (US POs, e.g. '06/16/2026' -> '16/06/2026', '11/02/2026' -> '02/11/2026')
    - DD/MM/YYYY (EU/Global POs, e.g. '15/07/2025' -> '15/07/2025')
    - YYYY-MM-DD (ISO format, e.g. '2026-06-16' -> '16/06/2026')
    - DD.MM.YYYY or YYYY.MM.DD (Dot format, e.g. '16.06.2026' -> '16/06/2026')
    - Textual dates (e.g. 'June 16, 2026' or '16 June 2026' -> '16/06/2026')
    - Korean dates (e.g. '2025년 03월 15일' -> '15/03/2025')

    Guarantees: Output is ALWAYS DD/MM/YYYY with valid DD (01-31) and MM (01-12).
    """
    if not date_str or not str(date_str).strip():
        return ""
    d = str(date_str).strip()

    # Zero/null placeholder check
    if _is_null_date(d):
        return ""

    # Korean date format: YYYY년 MM월 DD일
    m_kr = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", d)
    if m_kr:
        yyyy, mm, dd = m_kr.groups()
        return f"{dd.zfill(2)}/{mm.zfill(2)}/{yyyy}"

    # Korean compact: YYYY.MM.DD or YY.MM.DD
    m_krc = re.match(r"^(\d{4}|\d{2})\.(\d{1,2})\.(\d{1,2})$", d)
    if m_krc:
        yy, mm, dd = m_krc.groups()
        if len(yy) == 2:
            yy = "20" + yy
        if len(yy) == 4 and int(yy) > 1900:
            return f"{dd.zfill(2)}/{mm.zfill(2)}/{yy}"

    # Textual month formats, e.g. "June 16, 2026", "16 June 2026", "Jun 16 2026"
    m_txt = re.search(r"([a-zA-Z]+)\s+(\d{1,2})[,\s]+(\d{4})", d)
    if m_txt:
        mon_str, day_str, yr_str = m_txt.groups()
        mon_num = _MONTH_NAMES_MAP.get(mon_str.lower())
        if mon_num:
            return f"{day_str.zfill(2)}/{str(mon_num).zfill(2)}/{yr_str}"

    m_txt2 = re.search(r"(\d{1,2})\s+([a-zA-Z]+)[,\s]+(\d{4})", d)
    if m_txt2:
        day_str, mon_str, yr_str = m_txt2.groups()
        mon_num = _MONTH_NAMES_MAP.get(mon_str.lower())
        if mon_num:
            return f"{day_str.zfill(2)}/{str(mon_num).zfill(2)}/{yr_str}"

    # ISO format: YYYY-MM-DD or YYYY/MM/DD or YYYY.MM.DD
    m_iso = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", d)
    if m_iso:
        yyyy, mm, dd = m_iso.groups()
        return f"{dd.zfill(2)}/{mm.zfill(2)}/{yyyy}"

    # Two-part day/month + year: A/B/YYYY or A-B-YYYY or A.B.YYYY
    m_2p = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", d)
    if m_2p:
        p1, p2, yy = m_2p.groups()
        if len(yy) == 2:
            yy = "20" + yy
        val1 = int(p1)
        val2 = int(p2)

        # Unambiguous case 1: val2 > 12 (e.g. 06/16/2026 -> val2=16 is Day, val1=6 is Month)
        if val2 > 12 and val1 <= 12:
            # Input is MM/DD/YYYY -> Convert to DD/MM/YYYY (val2/val1/yy)
            return f"{str(val2).zfill(2)}/{str(val1).zfill(2)}/{yy}"

        # Unambiguous case 2: val1 > 12 (e.g. 16/06/2026 -> val1=16 is Day, val2=6 is Month)
        if val1 > 12 and val2 <= 12:
            # Input is ALREADY DD/MM/YYYY (val1/val2/yy)
            return f"{str(val1).zfill(2)}/{str(val2).zfill(2)}/{yy}"

        # Ambiguous case: both val1 <= 12 and val2 <= 12 (e.g. 06/11/2026 or 11/02/2026)
        if is_us_format:
            # US PO format: val1 is Month (MM), val2 is Day (DD) -> Convert to DD/MM/YYYY (val2/val1/yy)
            return f"{str(val2).zfill(2)}/{str(val1).zfill(2)}/{yy}"
        else:
            # European/Global format: val1 is Day (DD), val2 is Month (MM) -> (val1/val2/yy)
            return f"{str(val1).zfill(2)}/{str(val2).zfill(2)}/{yy}"

    return d



# Null/placeholder date patterns produced by ABB's PDF form renderer
_NULL_DATE_PATTERNS = re.compile(
    r'^(0+[/.-]0+[/.-]0+|0+[/.-]00[/.-]00|0/00/00|00/00/0000|0/0/0|0000-00-00)$'
)  

def _is_null_date(date_str: str) -> bool:
    """Return True if the date string is a placeholder zero-date from ABB-style PDF form fields."""
    if not date_str:
        return True
    return bool(_NULL_DATE_PATTERNS.match(date_str.strip()))


def _apply_regex_fallbacks(raw_text: str, extracted: dict) -> dict:
    """Apply regex patterns to fill in missing fields."""
    if not extracted:
        extracted = {"header_fields": {}, "line_items": []}

    header = extracted.get("header_fields", {})
    if not header:
        header = {}
        extracted["header_fields"] = header

    # -- Validate Extracted PO Number --
    po_val = header.get("po_number")
    if po_val:
        po_val_clean = po_val.strip().upper()
        # Invalid bank/payment/hallucination tokens that are NOT PO numbers
        INVALID_PO_TOKENS = {
            "DEUTSCHE", "DEUTSCHE BANK", "BNP", "PARIBAS", "UNICREDIT", "INTESA",
            "SANPAOLO", "COMMERZBANK", "CITIBANK", "HSBC", "BONIFICO", "BANK",
            "SWIFT", "IBAN", "CREDIT", "CREDITO", "SPORTELLO", "MILANO", "ROMA",
            "RIBA", "CONTANTI", "CASH", "CHEQUE", "ASSEGNO", "STBUS", "POSTBUS",
            "1,000", "1000", "OP"
        }
        if (po_val_clean in INVALID_PO_TOKENS
            or any(token in po_val_clean for token in ["DEUTSCHE BANK", "VS. BANCA", "VS BANCA", "BONIFICO"])
            or len(po_val_clean) == 0):
            print(f"  [BankPOGuard] Rejected invalid bank name/token as PO number: {repr(po_val)}")
            header["po_number"] = None
        # ITALIAN PO GUARD: reject values that look like a COD. FORN. (supplier code)
        # Pattern: mixed alpha+digits like '221LA112', '198AB023', 'A12LA023' — typical Italian supplier codes
        elif re.match(r'^[0-9]{1,4}[A-Z]{1,4}[0-9]{1,6}$', po_val_clean.replace(' ', ''), re.IGNORECASE):
            print(f"  [ItalianPOGuard] Rejected likely COD.FORN. supplier code as PO number: {repr(po_val_clean)}")
            header["po_number"] = None

    # -- Explicit email body text fallbacks for Sold-To, Ship-To, and PO Number --
    if raw_text:
        # Sold to ID fallback (e.g. "Sold to party code: 4020033653", "Sold to: 4020033653")
        sold_to_match = re.search(r'(?:Sold\s*to\s*(?:party)?\s*(?:code)?|Sold-to(?:\s*party)?(?:\s*code)?)\s*[:=]?\s*([0-9]{8,10})', raw_text, re.IGNORECASE)
        if sold_to_match:
            sid = sold_to_match.group(1).strip()
            header["sold_to_id"] = sid
            header["customer_number"] = sid
            print(f"  [HeaderFallback] Recovered Sold-To ID from text: '{sid}'")

        # Ship to ID fallback (e.g. "Ship to party code: 4020033653", "Ship to: 4020033653")
        ship_to_match = re.search(r'(?:Ship\s*to\s*(?:party)?\s*(?:code)?|Ship-to(?:\s*party)?(?:\s*code)?)\s*[:=]?\s*([0-9]{8,10})', raw_text, re.IGNORECASE)
        if ship_to_match:
            stid = ship_to_match.group(1).strip()
            header["ship_to_id"] = stid
            print(f"  [HeaderFallback] Recovered Ship-To ID from text: '{stid}'")

        # Explicit "PO no is “...”" or "PO no: ..."
        if not header.get("po_number"):
            po_explicit_match = re.search(r'(?:PO\s*no\s*(?:is)?|PO\s*number\s*(?:is)?)\s*[:=]?\s*[“"\'\s]*([^”"\'\n\r]+)[”"\'\s]*', raw_text, re.IGNORECASE)
            if po_explicit_match:
                p_cand = po_explicit_match.group(1).strip()
                if p_cand and len(p_cand) <= 40:
                    header["po_number"] = p_cand
                    print(f"  [HeaderFallback] Recovered PO number from explicit text pattern: '{p_cand}'")

    # -- PO Number fallback --
    if not header.get("po_number"):
        po_patterns = [
            # Contract No e.g. "Contract No.: CSG0152081"
            r"(?:Contract\s*(?:No|#)?|Purchase\s*Contract|Agreement\s*(?:No|#)?)\s*[:.]?\s*([A-Z0-9-]{4,25})",
            # Italian document number e.g. "Numero documento ... 97" or "Numero documento: 97"
            r"(?:Numero\s*documento|N[°o]\s*documento|Numero\s*Ordine|Ordine\s*N[°o]?)\s*[:.]?\s*.*?([0-9]{1,10})\b",
            # Specifically for Parker (e.g. 66090745 OP)
            r"(?:Purchase\s*Order|P\.?O\.?\s*(?:No|#)?|Order\s*(?:No|#)?|Ordine\s*(?:N\.?|#)?|Commande\s*(?:N[°o]?)?)\s*[:.]?\s*([0-9]{6,}\s*OP)",
            # General fallback (support short document numbers like 97)
            r"(?:Purchase\s*Order|P\.?O\.?\s*(?:No|#)?|Order\s*(?:No|#)?|Ordine\s*(?:N\.?|#)?|Commande\s*(?:N[°o]?)?)\s*[:.]?\s*([A-Z0-9][\w-]{0,20})",
        ]
        for pat in po_patterns:
            match = re.search(pat, raw_text, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                cand_clean = candidate.upper()
                if (cand_clean not in ["DEUTSCHE", "BANK", "BONIFICO"]
                    and len(candidate) <= 20
                    and not candidate.startswith("0000")):
                    header["po_number"] = candidate
                    print(f"  [POFallback] Recovered PO number from text: '{candidate}'")
                    break

    # -- Address post-processing: Reject Seller address as Ship-To --
    ship_to = header.get("ship_to_address") or ""
    if ship_to and any(s in ship_to.upper() for s in ["ENVALIOR INDIA", "ENVALIOR SINGAPORE", "ENVALIOR DEUTSCHLAND", "RANJANGAON", "MIDC RANJANGAON"]):
        print(f"  [AddressFix] Rejected Seller (Envalior) address in ship_to_address: {repr(ship_to[:60])}")
        consignee = header.get("consignee_address")
        bill_to = header.get("bill_to_address")
        header["ship_to_address"] = consignee or bill_to or ""

    # -- Date fallback --
    # First: clear placeholder zero-dates produced by ABB-style PDF form renderers
    # (e.g. '0/00/00', '00/00/0000') so the fallback regex below can fire.
    if _is_null_date(header.get('order_date', '')):
        header['order_date'] = ''
        print("  [DateFix] Cleared null/zero placeholder from order_date — running fallback regex.")

    if not header.get("order_date"):
        # Pattern 1: Explicit label (all languages + ABB 'P/O Date' + Korean labels)
        date_pat = (
            r"(?:P/?O\s*Date|Order\s*Date|Document\s*Date|Bestelldatum"
            r"|Date\s*de\s*commande|订单日期"
            r"|발주일|주문일|발주\s*일자|주문\s*일자"
            r"|발행일|작성일)\s*[:.]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
        )
        match = re.search(date_pat, raw_text, re.IGNORECASE)
        if match:
            header["order_date"] = _normalize_date(match.group(1))
            print(f"  [DateFix] order_date recovered via label regex: {header['order_date']!r}")
        else:
            # Pattern 1b: Korean year/month/day format (e.g. '2025년 03월 15일')
            kor_date_pat = r"(?:발주일|주문일|발주\s*일자|주문\s*일자|발행일|작성일)\s*[:.]?\s*(\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)"
            match = re.search(kor_date_pat, raw_text, re.IGNORECASE)
            if match:
                header["order_date"] = _normalize_date(match.group(1))
                print(f"  [DateFix] order_date recovered via Korean 년월일 pattern: {header['order_date']!r}")
            else:
                # Pattern 2: ABB-specific — short DD/MM/YY date appearing just before payment terms
                # In ABB PDFs the sequence is: <VAT> <date> <payment_terms> <PO_number>
                # e.g. "GB 232819270\n25/03/26\n90 DAYS EOM\n1368568"
                abb_pat = r"(\d{1,2}/\d{1,2}/\d{2,4})\s*\n?\s*(?:\d+\s+DAYS|NET\s+\d+|EOM|DUE\s+NET)"
                match = re.search(abb_pat, raw_text, re.IGNORECASE)
                if match:
                    header["order_date"] = _normalize_date(match.group(1))
                    print(f"  [DateFix] order_date recovered via ABB payment-terms pattern: {header['order_date']!r}")
                else:
                    # Pattern 3: Very loose fallback for any date
                    date_pat_loose = r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b"
                    dates = re.findall(date_pat_loose, raw_text)
                    if dates:
                        header["order_date"] = _normalize_date(dates[0])

    if not header.get("requested_delivery_date"):
        # Korean delivery date labels: 납기일, 납기, 납품기일, 배송일 + standard labels
        rdd_pat = (
            r"(?:Requested\s*Delivery\s*Date|RDD|Delivery\s*Date|Liefertermin"
            r"|Date\s*de\s*livraison|交货日期"
            r"|납기일|납기|납품\s*기일|납기\s*일자|배송일)\s*[:.]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
        )
        match = re.search(rdd_pat, raw_text, re.IGNORECASE)
        if match:
            header["requested_delivery_date"] = _normalize_date(match.group(1))
            print(f"  [DateFix] requested_delivery_date recovered via regex: {header['requested_delivery_date']!r}")
        else:
            # Korean 년월일 delivery date
            kor_rdd_pat = r"(?:납기일|납기|납품\s*기일|납기\s*일자|배송일)\s*[:.]?\s*(\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)"
            match = re.search(kor_rdd_pat, raw_text, re.IGNORECASE)
            if match:
                header["requested_delivery_date"] = _normalize_date(match.group(1))
                print(f"  [DateFix] requested_delivery_date recovered via Korean 년월일: {header['requested_delivery_date']!r}")

    # -- Clean quantities in line items --
    for item in extracted.get("line_items", []):
        if item.get("quantity"):
            item["quantity"] = _clean_quantity(item["quantity"])
        if item.get("delivery_date"):
            item["delivery_date"] = _normalize_date(item["delivery_date"])

    # -- FIX 2: Brand/Grade Name Promotion --
    # Safety net: if material_code is still empty after LLM extraction,
    # but material_description contains a recognised brand/grade pattern
    # (e.g. "Akulon F-X22092", "Durethan BKV30H2.0"), promote it to material_code.
    # This catches cases where the LLM still omits the code despite the prompt fix.
    #
    # Pattern covers Envalior brand families:
    #   Akulon, Durethan, Stanyl, Pocan, Xytron, Tepex, Arnitel, Eco-B, EcoPaXX
    _BRAND_GRADE_PATTERN = re.compile(
        r"\b(Akulon|Durethan|Stanyl|Pocan|Xytron|Tepex|Arnitel|Eco[- ]?B|EcoPaXX)"
        r"[\s\-]?[\w.\-/]+",
        re.IGNORECASE
    )

    for item in extracted.get("line_items", []):
        mat_code = (item.get("material_code") or "").strip()
        mat_desc = (item.get("material_description") or "").strip()

        # Only act when material_code is missing or was cleared (e.g. was a weight value)
        if not mat_code and mat_desc:
            brand_match = _BRAND_GRADE_PATTERN.search(mat_desc)
            if brand_match:
                promoted = brand_match.group(0).strip()
                item["material_code"] = promoted
                print(f"  [FIX2] Promoted brand/grade to material_code: '{promoted}'")

        # Also try raw_text scan if both code and description are empty
        if not (item.get("material_code") or "").strip() and raw_text:
            brand_match = _BRAND_GRADE_PATTERN.search(raw_text)
            if brand_match:
                promoted = brand_match.group(0).strip()
                item["material_code"] = promoted
                if not item.get("material_description"):
                    item["material_description"] = promoted
                print(f"  [FIX2] Recovered brand/grade from raw text: '{promoted}'")

    # Auto-detect US PO date format (e.g. EVCO Plastics, US addresses, US states)
    raw_upper = str(raw_text or "").upper()
    cust_upper = str(header.get("customer_name") or "").upper()
    addr_upper = (str(header.get("bill_to_address") or "") + " " + str(header.get("ship_to_address") or "")).upper()

    is_european = any(
        kw in raw_upper or kw in cust_upper or kw in addr_upper
        for kw in ("ITALY", "ITALIA", "MILANO", "GERMANY", "DEUTSCHLAND", "FRANCE", "SPAIN", "ESPAÑA", "NETHERLANDS", "ROMANIA", "ROUMANIA")
    )

    is_us_date = False if is_european else (
        any(
            kw in raw_upper or kw in cust_upper or kw in addr_upper
            for kw in ("UNITED STATES", "U.S.A", "EVCO PLASTICS", "DEFOREST WI", "EVANSVILLE IN")
        ) or any(st in addr_upper for st in (" WI ", " TX ", " CA ", " OH ", " MI ", " NC ", " SC ", " GA ", " FL ", " PA ", " IN "))
    )

    # -- Normalize header & item dates --
    if header.get("order_date"):
        header["order_date"] = _normalize_date(header["order_date"], is_us_format=is_us_date)
    if header.get("requested_delivery_date"):
        header["requested_delivery_date"] = _normalize_date(header["requested_delivery_date"], is_us_format=is_us_date)

    line_items = extracted.get("line_items", [])
    for item in line_items:
        if item.get("delivery_date"):
            item["delivery_date"] = _normalize_date(item["delivery_date"], is_us_format=is_us_date)

    return extracted



def _extract_postcode_from_address(address: str) -> str:
    """Extract a postcode from a free-text address string using regex."""
    if not address:
        return ""
    # Match common postcode patterns: 5-digit US/MX/EU, 4-digit NL/BE, alphanumeric UK, SG 6-digit
    patterns = [
        r'\b(\d{5,6})\b',               # 5 or 6-digit (MX/US/SG)
        r'\b([A-Z]{1,2}\d{1,2}\s?\d[A-Z]{2})\b',  # UK postcode
        r'\b(\d{4}\s?[A-Z]{2})\b',      # Dutch postcode
        r'\b(\d{4,5}[-\s]\d{3})\b',     # PT/BR postcode
    ]
    for pat in patterns:
        m = re.search(pat, address, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _post_process(extracted: dict) -> dict:
    """Clean up and normalize the extracted data."""
    header = extracted.get("header_fields", {})

    # -- Reject weight/quantity in material_code --
    for item in extracted.get("line_items", []):
        mc = item.get("material_code", "")
        if mc and _is_weight_as_material_code(mc):
            # LLM wrongly put qty+unit in material_code; clear it so mapper uses material_description
            item["material_code"] = ""
            if item.get("material_description") and not _is_weight_as_material_code(item["material_description"]):
                pass  # Mapper will use material_description when mat_code is empty

    # -- Promote Consignee address → ship_to when ship_to is empty ─────────
    # Some POs (Nagase Singapore etc.) use 'Consignee 1' instead of 'Ship To'.
    # If the LLM extracted a consignee address but no ship_to_address, promote it.
    ship_to  = (header.get("ship_to_address") or "").strip()
    consignee = (header.get("consignee_address") or "").strip()
    if not ship_to and consignee:
        header["ship_to_address"] = consignee
        # Also promote postcode
        consignee_pc = (header.get("consignee_postcode") or "").strip()
        if not header.get("ship_to_postcode") and consignee_pc:
            header["ship_to_postcode"] = consignee_pc
        elif not header.get("ship_to_postcode"):
            # Try to extract postcode from consignee address text
            derived_pc = _extract_postcode_from_address(consignee)
            if derived_pc:
                header["ship_to_postcode"] = derived_pc
        print(f"  [PostProc] No ship_to — promoted Consignee address: '{consignee[:60]}...' (postcode: {header.get('ship_to_postcode', 'N/A')})")

    # -- If ship_to still empty, try to extract postcode from it ───────────
    if header.get("ship_to_address") and not header.get("ship_to_postcode"):
        derived_pc = _extract_postcode_from_address(header["ship_to_address"])
        if derived_pc:
            header["ship_to_postcode"] = derived_pc
            print(f"  [PostProc] Derived ship_to_postcode '{derived_pc}' from ship_to_address.")

    # Clean whitespace from addresses
    for key in ["ship_to_address", "sold_to_address", "consignee_address", "bill_to_address"]:
        if header.get(key):
            header[key] = re.sub(r"\s+", " ", header[key]).strip()

    # Clean vendor/customer names
    for key in ["customer_id_or_name", "vendor_name"]:
        if header.get(key):
            header[key] = header[key].strip()

    # -- Set Requested Delivery Date to Earliest Item Date --
    import datetime
    line_items = extracted.get("line_items", [])
    valid_dates = []

    for item in line_items:
        d_str = item.get("delivery_date")
        if d_str:
            try:
                dt = datetime.datetime.strptime(d_str, "%d/%m/%Y")
                valid_dates.append(dt)
            except ValueError:
                pass

    if valid_dates:
        earliest = min(valid_dates)
        header["requested_delivery_date"] = earliest.strftime("%d/%m/%Y")

    # Single item: if line delivery_date is empty, set to requested delivery date
    if len(line_items) == 1 and header.get("requested_delivery_date"):
        if not line_items[0].get("delivery_date"):
            line_items[0]["delivery_date"] = header["requested_delivery_date"]

    return extracted


# ------------------------------------------------
# Main Pipeline
# ------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Enhanced PO Extraction Pipeline")
    parser.add_argument("--file", type=str, help="Single file to process")
    parser.add_argument("--folder", type=str, help="Folder of files to process")
    parser.add_argument("--schema", type=str, default="po_schema.json")
    parser.add_argument("--output", type=str, default="results_enhanced.jsonl")
    parser.add_argument("--excel", type=str, default="EXPORT_20260216_100124.XLSX",
                        help="Excel mapping file for Sales Order generation")
    parser.add_argument("--ocr_input", type=str, help="Pre-computed OCR JSON file")
    args = parser.parse_args()

    # Load batch OCR if provided
    batch_ocr_data = None
    if args.ocr_input and os.path.exists(args.ocr_input):
        print(f"Loading batch OCR data from {args.ocr_input}...")
        with open(args.ocr_input, 'r', encoding='utf-8') as f:
            batch_ocr_data = json.load(f)

    # Init LLM
    setup_llm()

    # Init Sales Order Mapper
    so_mapper = None
    if os.path.exists(args.excel):
        so_mapper = SalesOrderMapper(args.excel)
    else:
        print(f"Warning: Excel mapping file '{args.excel}' not found. Skipping SO generation.")

    # Pre-load vendor cache
    _load_vendor_cache()

    # Get files
    files = []
    if args.file:
        fpath = Path(args.file)
        if fpath.suffix.lower() in (".msg", ".eml"):
            print(f"  [EMAIL] Pre-processing single email file: {fpath.name}")
            if fpath.suffix.lower() == ".eml":
                extract_eml_file(fpath, fpath.parent)
            else:
                extract_msg_file(fpath, fpath.parent)
            # Find and add the extracted body text and attachments
            for ext in ["png", "jpg", "jpeg", "pdf", "txt"]:
                files.extend(fpath.parent.glob(f"{fpath.stem}_body.txt"))
                files.extend(fpath.parent.glob(f"{fpath.stem}_att_*.{ext}"))
        else:
            files.append(fpath)
    elif args.folder:
        p = Path(args.folder)
        # First preprocess all .msg and .eml files recursively in the folder
        preprocess_msg_files(p)
        # Now scan for standard files (excl. msg/eml)
        for ext in ["png", "jpg", "jpeg", "pdf", "txt", "xlsx", "xls"]:
            files.extend(p.rglob(f"*.{ext}"))

    # ------------------------------------------------------------------
    # FILTER: Remove generic email inline-image attachments.
    # Files named image.png / image001.jpg / image_AAMkAGEz.png etc.
    # are ALWAYS email logos / signatures — never actual PO documents.
    # Processing them wastes Claude Vision tokens and fires false exceptions.
    # ------------------------------------------------------------------
    import re as _re
    _INLINE_IMAGE_PAT = _re.compile(
        r'^image\d*(_[A-Za-z0-9]+)?\.(png|jpg|jpeg)$',
        _re.IGNORECASE
    )
    before_filter = len(files)
    files = [f for f in files if not _INLINE_IMAGE_PAT.match(f.name)]
    skipped_images = before_filter - len(files)
    if skipped_images:
        print(f"  [Filter] Skipped {skipped_images} inline email image(s) (image*.png/jpg) — not PO documents.")

    # ------------------------------------------------------------------
    # FILTER: Skip email body .txt files when a PDF sibling already exists.
    # body_*.txt files are email body text saved alongside a PDF attachment.
    # When the PDF is present, the txt is a duplicate and causes a false
    # extraction with no sold_to_id / material mapping.
    # If no PDF sibling exists, keep the txt so text-only POs are still handled.
    # ------------------------------------------------------------------
    _filtered_txts = []
    _pdf_names_in_folder = {}  # folder -> set of pdf stems (lowercase)
    for _f in files:
        if _f.suffix.lower() == '.pdf':
            _pdf_names_in_folder.setdefault(str(_f.parent), set()).add(_f.stem.lower())

    for _f in list(files):
        if _f.suffix.lower() == '.txt' and _f.name.lower().startswith('body_'):
            # Check if any PDF exists in the same folder
            folder_pdfs = _pdf_names_in_folder.get(str(_f.parent), set())
            if folder_pdfs:
                _filtered_txts.append(_f.name)
                files.remove(_f)

    if _filtered_txts:
        print(f"  [Filter] Skipped {len(_filtered_txts)} email body .txt file(s) — PDF sibling exists, txt is duplicate:")
        for _n in _filtered_txts:
            print(f"           {_n}")

    # ------------------------------------------------------------------
    # FILTER: Skip system-generated exception / notification emails.
    # Files whose names start with '[PO Exception', '[po exception',
    # '[robona-attach]', 'undeliverable' or contain 'ai processing failed'
    # are pipeline self-generated notifications — NOT real customer POs.
    # Re-processing them creates an infinite loop of false exception emails.
    # ------------------------------------------------------------------
    _EXCEPTION_PREFIXES = (
        '[po exception',
        '[robona-attach]',
        'undeliverable',
        'undeliverable:',
    )
    _EXCEPTION_SUBSTRINGS = (
        'ai processing failed',
        'po exception - unknown',
        'po exception -',
    )
    def _is_exception_file(p):
        name_lower = p.name.lower()
        if any(name_lower.startswith(pfx) for pfx in _EXCEPTION_PREFIXES):
            return True
        if any(sub in name_lower for sub in _EXCEPTION_SUBSTRINGS):
            return True
        return False

    before_exception_filter = len(files)
    files = [f for f in files if not _is_exception_file(f)]
    skipped_exceptions = before_exception_filter - len(files)
    if skipped_exceptions:
        print(f"  [Filter] Skipped {skipped_exceptions} system-generated exception/notification file(s) — not real PO documents.")

    if not files:
        print("No files found.")
        with open(args.output, "w", encoding="utf-8") as f:
            pass # Create empty file to prevent downstream crashing
        return

    print(f"\n{'='*60}")
    print(f"Processing {len(files)} files...")
    print(f"{'='*60}\n")

    results = []
    for fpath in files:
        # Secondary guard: skip any inline image that may have arrived via single-file path
        if _INLINE_IMAGE_PAT.match(fpath.name):
            print(f"  [Skip] {fpath.name} — inline email image, not a PO document.")
            continue

        print(f"\n-- Extracting: {fpath.name} --")

        # -- Step 1: Text Extraction (TXT = direct read; PDF/images = OCR) --
        raw_text = ""
        if batch_ocr_data and fpath.name in batch_ocr_data:
            raw_text = batch_ocr_data[fpath.name]
            print(f"  [OCR] Using pre-computed batch data.")
        elif fpath.suffix.lower() == ".txt":
            # Email body or plain-text PO: read directly (no OCR)
            try:
                raw_text = fpath.read_text(encoding="utf-8", errors="replace")
                print(f"  [TXT] Read {len(raw_text)} chars from plain text.")
            except Exception as e:
                print(f"  [TXT] Read error: {e}")
                continue
        elif fpath.suffix.lower() in (".xlsx", ".xls"):
            # ── Excel PO handler (.xlsx and legacy .xls) ─────────────────
            # Reads all cells and builds a structured text representation
            # for Claude — this preserves labels like "Customer PO number"
            # so Claude can capture text-based PO refs like "ASOS Order September".
            try:
                import datetime as _dt
                cell_data = {}

                if fpath.suffix.lower() == ".xls":
                    import xlrd
                    import openpyxl
                    wb = xlrd.open_workbook(str(fpath))
                    ws = wb.sheet_by_index(0)
                    for r_idx in range(ws.nrows):
                        for c_idx in range(ws.ncols):
                            val = ws.cell_value(r_idx, c_idx)
                            if val not in (None, ""):
                                c_type = ws.cell_type(r_idx, c_idx)
                                if c_type == xlrd.XL_CELL_DATE:
                                    try:
                                        dt_tuple = xlrd.xldate_as_tuple(val, wb.datemode)
                                        val = f"{dt_tuple[2]:02d}/{dt_tuple[1]:02d}/{dt_tuple[0]}"
                                    except Exception:
                                        pass
                                elif isinstance(val, float) and val.is_integer():
                                    val = int(val)
                                col_letter = openpyxl.utils.get_column_letter(c_idx + 1)
                                coord = f"{col_letter}{r_idx + 1}"
                                cell_data[coord] = str(val).strip()
                else:
                    import openpyxl
                    wb = openpyxl.load_workbook(str(fpath), data_only=True)
                    ws = wb.active
                    for row in ws.iter_rows():
                        for cell in row:
                            if cell.value not in (None, ""):
                                val = cell.value
                                if isinstance(val, _dt.datetime):
                                    val = val.strftime("%d/%m/%Y")
                                cell_data[cell.coordinate] = val

                # Build flat text for Claude
                lines_out = [f"[Excel PO: {fpath.name}]"]
                for coord, val in cell_data.items():
                    lines_out.append(f"{coord}: {val}")
                raw_text = "\n".join(lines_out)
                print(f"  [Excel] Read {len(cell_data)} cells from '{fpath.name}' ({len(raw_text)} chars)")

                # Log detected PO number label for diagnostics
                for coord, val in cell_data.items():
                    str_val = str(val).strip().lower()
                    if "customer po number" in str_val or "klantreferentie" in str_val:
                        col_letter = "".join(filter(str.isalpha, coord))
                        row_num = int("".join(filter(str.isdigit, coord)))
                        next_col = chr(ord(col_letter[-1]) + 1)
                        po_cell = cell_data.get(f"{next_col}{row_num}")
                        if po_cell:
                            print(f"  [Excel] Detected Customer PO number field: '{po_cell}'")
            except Exception as xlsx_err:
                print(f"  [Excel] Error reading {fpath.name}: {xlsx_err}")
                continue
        else:
            # -- Try PyMuPDF native text first (digital PDFs) --
            if fpath.suffix.lower() == ".pdf":
                try:
                    doc = fitz.open(str(fpath))
                    native_pages = [page.get_text("text") for page in doc]
                    doc.close()
                    native_text = "\n\n".join(t for t in native_pages if t.strip())
                    if len(native_text) > 100:
                        raw_text = native_text
                        print(f"  [PyMuPDF] Got {len(raw_text)} chars of native text ✓")
                except Exception as e:
                    print(f"  [PyMuPDF] Native extraction failed: {e}")

            # -- Fallback Path A: Claude Vision for scanned/image-based PDFs --
            if not raw_text and LLM_PROVIDER == "claude" and claude_client:
                try:
                    print(f"  [Vision] PDF has no native text — rendering pages for Claude Vision...")
                    doc = fitz.open(str(fpath))
                    page_images = []
                    for page in doc:
                        mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for OCR quality
                        pix = page.get_pixmap(matrix=mat)
                        page_images.append(pix.tobytes("png"))
                    doc.close()
                    if page_images:
                        print(f"  [Vision] Sending {len(page_images)} page(s) to Claude Vision...")
                        vision_response = claude_client.extract_from_images(
                            image_bytes_list=page_images,
                            system_prompt=SYSTEM_PROMPT,
                            extra_text="Extract the Purchase Order data from the page image(s) above. Output ONLY the JSON object.",
                            media_type="image/png",
                        )
                        # Store as _VISION_DIRECT_ sentinel so we skip the text-based LLM call below
                        raw_text = "_VISION_DIRECT_"
                        # Extract JSON straight from vision response
                        extracted_data = _extract_json_from_text(vision_response)
                        if "error" not in extracted_data:
                            extracted_data["ocr_engine"] = "claude_vision"
                            print(f"  [Vision] Claude Vision extraction successful ✓")
                        else:
                            print(f"  [Vision] Claude Vision parse issue: {extracted_data.get('error')}")
                            raw_text = ""  # reset so we fall through to PaddleOCR
                            extracted_data = {}
                except Exception as e:
                    print(f"  [Vision] Claude Vision Error: {e}")
                    raw_text = ""

            # -- Fallback Path B: PaddleOCR (scanned PDFs/images — used if Claude Vision unavailable) --
            if not raw_text:
                try:
                    print(f"  [OCR] Running PaddleOCR subprocess...")
                    cmd = [sys.executable, "run_ocr_tool.py", "--file", str(fpath)]
                    child_env = os.environ.copy()
                    child_env["DISABLE_MODEL_SOURCE_CHECK"] = "True"
                    result = subprocess.run(
                        cmd, capture_output=True, text=True,
                        encoding='utf-8', errors='replace',
                        env=child_env, timeout=120
                    )
                    if result.returncode != 0:
                        print(f"  [OCR] Error: {result.stderr}")
                    else:
                        raw_text = result.stdout.strip()
                except subprocess.TimeoutExpired:
                    print(f"  [OCR] PaddleOCR timed out after 120s for {fpath.name}")
                except Exception as e:
                    print(f"  [OCR] Execution Error: {e}")

        # -- Handle image files (PNG/JPG) via Claude Vision if no text yet --
        if not raw_text and fpath.suffix.lower() in (".png", ".jpg", ".jpeg"):
            if LLM_PROVIDER == "claude" and claude_client:
                try:
                    print(f"  [Vision] Sending image file to Claude Vision...")
                    img_bytes = fpath.read_bytes()
                    ext = fpath.suffix.lower()
                    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
                    vision_response = claude_client.extract_from_images(
                        image_bytes_list=[img_bytes],
                        system_prompt=SYSTEM_PROMPT,
                        extra_text="Extract the Purchase Order data from the image above. Output ONLY the JSON object.",
                        media_type=mime,
                    )
                    raw_text = "_VISION_DIRECT_"
                    extracted_data = _extract_json_from_text(vision_response)
                    if "error" not in extracted_data:
                        extracted_data["ocr_engine"] = "claude_vision"
                        print(f"  [Vision] Claude Vision extraction successful ✓")
                    else:
                        print(f"  [Vision] Claude Vision parse issue: {extracted_data.get('error')}")
                        raw_text = ""
                        extracted_data = {}
                except Exception as e:
                    print(f"  [Vision] Claude Vision Error for image: {e}")

        # Attach sibling email body context if present in folder
        if fpath.suffix.lower() != ".txt":
            sibling_txts = list(fpath.parent.glob("body_*.txt"))
            if sibling_txts:
                try:
                    body_content = sibling_txts[0].read_text(encoding="utf-8", errors="replace").strip()
                    if body_content:
                        raw_text = (raw_text or "") + f"\n\n[Email Body Context]\n{body_content}"
                        print(f"  [EmailContext] Attached sibling email body text ({len(body_content)} chars)")
                except Exception:
                    pass

        if not raw_text:
            results.append({"source_file": fpath.name, "error": "Empty OCR"})
            continue

        if raw_text == "_VISION_DIRECT_":
            print(f"  [Extract] Vision direct extraction — skipping text-based LLM call.")
        else:
            print(f"  [Extract] Text length: {len(raw_text)} chars")

        # -- Step 2: LLM Extraction (skipped when Vision already populated extracted_data) --
        extracted_data_from_vision = raw_text == "_VISION_DIRECT_"
        if not extracted_data_from_vision:
            extracted_data = {}
            if LLM_PROVIDER == "claude" and claude_client:
                prompt = PROMPT_TEMPLATE_CLAUDE.format(content=raw_text)
                try:
                    print(f"  [Claude] Generating extraction...")
                    response_str = claude_client.extract_data(prompt, system_prompt=SYSTEM_PROMPT)
                    extracted_data = _extract_json_from_text(response_str)
                    if "error" in extracted_data:
                        print(f"  [Claude] Parse issue: {extracted_data.get('error')}")
                except Exception as e:
                    print(f"  [Claude] Error: {e}")
                    extracted_data = {"error": str(e)}
            elif llm_pipeline:
                prompt = PROMPT_TEMPLATE_LOCAL.format(content=raw_text)
                try:
                    print(f"  [Local LLM] Generating extraction...")
                    response_str = llm_pipeline.invoke(prompt)
                    extracted_data = _extract_json_from_text(response_str)
                    if "error" in extracted_data:
                        print(f"  [Local LLM] Parse issue: {extracted_data.get('error')}")
                except Exception as e:
                    print(f"  [Local LLM] Error: {e}")
                    extracted_data = {"error": str(e)}
            else:
                extracted_data = {"error": "LLM provider or client not initialized"}

        # -- Fallback Step 2.5: If text extraction returned vendor (Envalior/DSM) as customer (e.g. image logo) --
        if not extracted_data_from_vision and fpath.suffix.lower() == ".pdf" and LLM_PROVIDER == "claude" and claude_client:
            header_temp = extracted_data.get("header_fields", {}) or {}
            c_name_temp = (
                header_temp.get("customer_name") 
                or header_temp.get("customer_id_or_name")
                or header_temp.get("customer_id")
                or ""
            ).lower()
            if "envalior" in c_name_temp or "dsm" in c_name_temp:
                print("  [Extract] WARNING: Extracted customer name contains vendor name 'Envalior/DSM' (likely image logo). Falling back to Claude Vision...")
                try:
                    doc = fitz.open(str(fpath))
                    page_images = []
                    for page in doc:
                        mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for OCR quality
                        pix = page.get_pixmap(matrix=mat)
                        page_images.append(pix.tobytes("png"))
                    doc.close()
                    if page_images:
                        print(f"  [Vision] Sending {len(page_images)} page(s) to Claude Vision...")
                        vision_response = claude_client.extract_from_images(
                            image_bytes_list=page_images,
                            system_prompt=SYSTEM_PROMPT,
                            extra_text="Extract the Purchase Order data from the page image(s) above. Output ONLY the JSON object.",
                            media_type="image/png",
                        )
                        vision_data = _extract_json_from_text(vision_response)
                        if "error" not in vision_data:
                            extracted_data = vision_data
                            extracted_data["ocr_engine"] = "claude_vision"
                            extracted_data_from_vision = True
                            print(f"  [Vision] Claude Vision fallback extraction successful ✓")
                        else:
                            print(f"  [Vision] Claude Vision fallback parse issue: {vision_data.get('error')}")
                except Exception as e:
                    print(f"  [Vision] Claude Vision Fallback Error: {e}")

        # -- Step 3: Filename Hints & Overrides --
        # Check for Sold-to ID in filename
        filename_sold_to = _extract_sold_to_from_filename(fpath.name)
        if filename_sold_to:
            print(f"  [Filename] Sold-to ID: {filename_sold_to} (overriding extraction)")
            header = extracted_data.setdefault("header_fields", {})
            header["customer_id_or_name"] = filename_sold_to
        
        # Check for Material IDs in filename
        filename_materials = _extract_material_ids_from_filename(fpath.name)
        if filename_materials:
            print(f"  [Filename] Material IDs for lookup: {filename_materials}")
            extracted_data["filename_materials"] = filename_materials

        # -- Step 4: Clean CJK unknown markers --
        extracted_data = _clean_unknown_values(extracted_data)

        # -- Step 5: Regex Fallbacks --
        # Always run regex fallbacks; for vision results, we still need to recover missing dates
        # (Korean POs often have dates only in the footer or in Korean label format that the LLM missed).
        if not extracted_data_from_vision:
            extracted_data = _apply_regex_fallbacks(raw_text, extracted_data)
        else:
            # Vision path: run date-only regex recovery on the raw_text we still have
            # (raw_text may be empty for pure vision paths, but try regardless)
            _raw_for_date_fix = raw_text if raw_text and raw_text != "_VISION_DIRECT_" else ""
            if _raw_for_date_fix:
                extracted_data = _apply_regex_fallbacks(_raw_for_date_fix, extracted_data)

        # -- Step 6: Clean PO number (strip trailing slash / backslash artifacts) --
        hf = extracted_data.get("header_fields", {})
        raw_po = hf.get("po_number", "")
        if raw_po:
            cleaned_po = raw_po.strip().rstrip("/\\").strip()
            if cleaned_po != raw_po:
                print(f"  [Post] Cleaned PO number: '{raw_po}' -> '{cleaned_po}'")
                hf["po_number"] = cleaned_po
        # -- Step 7: Post-process (normalise units, fields etc.) --
        extracted_data = _post_process(extracted_data)

        # -- Step 8: Knowledge Base Enrichment --
        header = extracted_data.get("header_fields", {})

        # Claude uses different field names depending on PO format; check all possibilities
        v_name = header.get("vendor_name") or header.get("supplier_name")
        c_name = (
            header.get("customer_id_or_name")
            or header.get("customer_name")
            or header.get("customer_id")
            or header.get("buyer_name")
        )
        # Normalize: if we found a customer name, stash it so downstream code always sees the same key
        if c_name and not header.get("customer_id_or_name"):
            header["customer_id_or_name"] = c_name

        # Build address hint for SOLD-TO (customer) matching.
        # IMPORTANT: Use BILL TO postcode for customer (sold-to) disambiguation —
        # the sold-to postcode in SAP is the billing address, NOT the delivery address.
        # ship_to_postcode is used separately for ship-to resolution below.
        bill_to_pc = header.get("bill_to_postcode") or ""
        # ship_to_postcode may have been set by post-processing (Consignee promotion / footer extraction)
        ship_to_pc = header.get("ship_to_postcode") or header.get("consignee_postcode") or ""
        addr_parts = [
            header.get("bill_to_address", ""),   # Primary: billing/sold-to address
            header.get("sold_to_address", ""),
            bill_to_pc,                           # Billing postcode for sold-to match
            header.get("country", ""),
            header.get("city", ""),
            # Include ship_to_address as secondary context (city name helps)
            header.get("ship_to_address", ""),
            header.get("consignee_address", ""),  # Consignee = ship-to for some PO formats
        ]
        address_hint = " ".join(p for p in addr_parts if p).strip()
        # Store normalised postcodes back so downstream ship-to matching uses them
        header["_bill_to_postcode"] = bill_to_pc.strip()
        header["_ship_to_postcode"] = ship_to_pc.strip()

        # Fuzzy match vendor
        if v_name:
            match = fuzzy_match_vendor(
                v_name,
                line_items=extracted_data.get("line_items", []),
                address_hint=address_hint,
            )
            if match:
                print(f"  [KB] Vendor '{v_name}' -> '{match['name']}' (ID: {match['id']}, score: {match['score']})")
                header["vendor_name_matched"] = match["name"]
                header["vendor_id"] = match["id"]
                # Overwrite the LLM output with the golden KB name
                header["vendor_name"] = match["name"]

        # Fuzzy match customer
        if c_name and c_name != v_name:
            c_match = fuzzy_match_vendor(
                c_name,
                line_items=extracted_data.get("line_items", []),
                address_hint=address_hint,
            )
            # ── Material-code fallback ─────────────────────────────────────
            # When trade name / abbreviation on PO fails fuzzy match
            # (e.g. 'JC NORTH AMERICA' vs 'Junchuang North America Inc'),
            # try to identify the customer uniquely via their material code.
            if not c_match:
                from reenrich_results import _match_customer_by_material
                c_match = _match_customer_by_material(extracted_data.get("line_items", []), customer_name=c_name or "")
                if c_match:
                    print(f"  [KB] Name match failed for '{c_name}' — resolved via material code fallback")
            if c_match:
                print(f"  [KB] Customer '{c_name}' -> '{c_match['name']}' (ID: {c_match['id']}, score: {c_match['score']})")
                # Overwrite the LLM typo with the golden KB name
                header["customer_number"] = c_match["id"]
                header["customer_name_matched"] = c_match["name"]
                header["customer_id_or_name"] = c_match["name"]

                # Validate Ship/Sold To using CUSTOMER ID (not vendor ID)
                val_res = validate_ship_sold_to(
                    c_match["id"],
                    header.get("ship_to_address", ""),
                    header.get("sold_to_address", ""),
                    ship_to_postcode=header.get("_ship_to_postcode", "") or header.get("ship_to_postcode", ""),
                    raw_text=raw_text,
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

        # Look up sender_email from tracking database if available
        try:
            import sqlite3
            db_p = Path(__file__).parent / "processed_emails.db"
            if db_p.exists():
                _conn = sqlite3.connect(str(db_p))
                _cur = _conn.cursor()
                _cur.execute("SELECT sender_email FROM processed_emails WHERE source_file = ? OR attachments LIKE ?", (fpath.name, f"%{fpath.name}%"))
                _r = _cur.fetchone()
                if _r and _r[0]:
                    header["sender_email"] = _r[0]
                _conn.close()
        except Exception:
            pass

        # -- Step 6: Sales Order Generation --
        sales_orders = []
        if so_mapper:
            filename_mats = extracted_data.get("filename_materials")
            sales_orders = so_mapper.generate_sales_orders(extracted_data, filename_materials=filename_mats)
            if sales_orders:
                print(f"  [SO] Generated {len(sales_orders)} Sales Order(s)")
            else:
                print(f"  [SO] No mapping found for this customer/material combination")

        # -- Compile Result --
        extracted_data["source_file"] = fpath.name
        # Preserve claude_vision label if set by vision path; fallback to paddleocr for PaddleOCR path
        if "ocr_engine" not in extracted_data:
            extracted_data["ocr_engine"] = "paddleocr"
        extracted_data["sales_orders"] = sales_orders
        results.append(extracted_data)

        # Print summary
        h = extracted_data.get("header_fields", {})
        items = extracted_data.get("line_items", [])
        print(f"  [Result] PO: {h.get('po_number', '?')} | Customer: {h.get('customer_name_matched') or h.get('customer_id_or_name', '?')} | Items: {len(items)} | SOs: {len(sales_orders)}")

    # -- Save Results --
    with open(args.output, "w", encoding="utf-8") as f:
        for res in results:
            # Remove raw_text from output (too large)
            res.pop("raw_text", None)
            f.write(json.dumps(res, ensure_ascii=False) + "\n")

    print(f"\n{'='*60}")
    print(f"Done! Saved {len(results)} results to {args.output}")
    print(f"{'='*60}")

    if results:
        print("\nSample result:")
        sample = {k: v for k, v in results[0].items() if k != "raw_text"}
        print(json.dumps(sample, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
