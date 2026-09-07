import os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

wb = openpyxl.Workbook()

# Define styles
header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
title_font = Font(name="Calibri", size=14, bold=True, color="1F4E79")
sub_font = Font(name="Calibri", size=10, italic=True, color="595959")
data_font = Font(name="Calibri", size=10)
bold_font = Font(name="Calibri", size=10, bold=True)

pass_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid") # soft green
pass_font = Font(name="Calibri", size=10, bold=True, color="375623")

warn_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid") # soft yellow
warn_font = Font(name="Calibri", size=10, bold=True, color="7F6000")

thin_border = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9")
)

# ═════════════════════════════════════════════════════════════════════════════
# SHEET 1: 10 Fresh POs (Current UAT Session)
# ═════════════════════════════════════════════════════════════════════════════
ws1 = wb.active
ws1.title = "Current Run - 10 Fresh POs"
ws1.views.sheetView[0].showGridLines = True

ws1["A1"] = "UAT Processing Report — 10 Fresh POs (Christian 26 - 35)"
ws1["A1"].font = title_font
ws1["A2"] = "Summary of extraction, master data mapping, SAP BAPI validation, and automated Roborana notification results."
ws1["A2"].font = sub_font

headers1 = [
    "#", "PO Number", "Customer Name", "PO Extraction", "Master Data Mapping", 
    "SAP Validation", "SO Created", "Overall Result", "Failure Ownership", "Exception / Comment"
]

for col_num, header in enumerate(headers1, 1):
    cell = ws1.cell(row=4, column=col_num, value=header)
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

data1 = [
    (
        1, "PO 26/312 (Christian 26)", "Blow Molding System, S.L", 
        "✓", "✓", "✓", "✓ (0001536839)", "PASS", "—",
        "Sales Order 0001536839 created touchlessly in SAP. Roborana notification email sent with both PO PDF (589 KB) and original .msg email (817 KB) attached."
    ),
    (
        2, "PO 60680 (Christian 27)", "Licharz GmbH",
        "✓", "✓", "⚠", "—", "EXCEPTION", "Envalior SAP Configuration",
        "Celonis Action Flow RFC error: [E:V1:117]: Material 0000053398 has been excluded in SALES_ITEM_IN 000010. Material listing/exclusion is active in SAP Sales Org 2540."
    ),
    (
        3, "PO 11417 (Christian 28)", "Tekuma Kunststoff GmbH",
        "✓", "⚠", "—", "—", "EXCEPTION", "Envalior Master Data",
        "Missing CMIR in SAP: Customer material '01 140311' not maintained in Test MP / SAP. Agent safely stopped order creation and routed exception to CSR."
    ),
    (
        4, "PO 2600087 (Christian 29)", "METROX - PLAST Sp. z o.o",
        "✓", "✓", "✓", "✓ (0001536841)", "PASS", "—",
        "Sales Order 0001536841 created touchlessly in SAP. Roborana notification email sent with both PO PDF (61 KB) and original .msg email (141 KB) attached."
    ),
    (
        5, "PO 4710833926-S-210/4461 (Christian 30)", "Evonik Operations GmbH",
        "✓", "✓", "⚠", "—", "EXCEPTION", "Envalior SAP Configuration",
        "Celonis Action Flow RFC error: [E:V1:117]: Material 0000053398 has been excluded in SALES_ITEM_IN 000010. Material listing/exclusion active in SAP Sales Org 2540."
    ),
    (
        6, "PO PP0074166 / NY24409 (Christian 31)", "WHS Plastics",
        "⚠", "—", "—", "—", "EXCEPTION", "Customer / Process",
        "Non-standard PO: Delivery schedule amendment table sent in HTML body without a PDF PO attached. Agent detected missing PO document and routed exception to CSR."
    ),
    (
        7, "PO EX-1031059/26 (Christian 32)", "Technoform Bautec Italia S.p.A",
        "✓", "✓", "✓", "✓ (0001536843)", "PASS", "—",
        "Sales Order 0001536843 created touchlessly in SAP. Roborana notification email sent with both PO PDF (368 KB) and original .msg email (512 KB) attached."
    ),
    (
        8, "PO 4500145024 (Christian 33)", "L. Brüggemann GmbH & Co. KG",
        "✓", "✓", "✓", "✓ (0001536842)", "PASS", "—",
        "Sales Order 0001536842 created touchlessly in SAP. Roborana notification email sent with both PO PDF (113 KB) and original .msg email (161 KB) attached."
    ),
    (
        9, "PO 135796 (Christian 34)", "TER HELL Plastic GmbH",
        "✓", "✓", "✓", "✓ (0001536844)", "PASS", "—",
        "Sales Order 0001536844 created touchlessly in SAP. Roborana notification email sent with both PO PDF (239 KB) and original .msg email (348 KB) attached."
    ),
    (
        10, "PO ZZ13551 (Christian 35)", "TEREZ Performance Polymers Sp.z o.o.",
        "✓", "⚠", "—", "—", "EXCEPTION", "Envalior Master Data",
        "Missing CMIR in SAP: Customer material 'R307891/T41133-00' unmapped in SAP CMIR. Agent routed exception email to CSR."
    )
]

for row_idx, row_data in enumerate(data1, start=5):
    for col_idx, val in enumerate(row_data, start=1):
        cell = ws1.cell(row=row_idx, column=col_idx, value=val)
        cell.font = data_font
        cell.border = thin_border
        
        # Center align short status columns
        if col_idx in [1, 4, 5, 6, 7]:
            cell.alignment = Alignment(horizontal="center", vertical="center")
        elif col_idx in [8, 9]:
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if val == "PASS":
                cell.fill = pass_fill
                cell.font = pass_font
            elif val == "EXCEPTION":
                cell.fill = warn_fill
                cell.font = warn_font
        else:
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

# ═════════════════════════════════════════════════════════════════════════════
# SHEET 2: 18 Historical Defects Analysis (Christian's Previous UAT Batch)
# ═════════════════════════════════════════════════════════════════════════════
ws2 = wb.create_sheet(title="Historical - 18 Defects")
ws2.views.sheetView[0].showGridLines = True

ws2["A1"] = "Root Cause & Status Analysis — 18 Defects Filed by Christian"
ws2["A1"].font = title_font
ws2["A2"] = "Detailed technical attribution (Agent / Celonis / Envalior Master Data / Customer) and resolution status."
ws2["A2"].font = sub_font

headers2 = [
    "Defect #", "Case / PO Reference", "Defect Description", 
    "Root Cause Identified (Whose Issue)", "Defect Status", "Resolution / Fix Deployed"
]

for col_num, header in enumerate(headers2, 1):
    cell = ws2.cell(row=4, column=col_num, value=header)
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

data2 = [
    (
        24, "Christian 6",
        "Customer uses term 'lead time' instead of 'delivery date'. Wrong customer determination (Pandrol UK vs Pandrol Iberica) + customer email attached in wrong format (unformatted txt with body_aamkagez_ prefix).",
        "Agent Refinement (Customer Group Disambiguation & Native .msg Format)",
        "Fixed",
        "New requirement: Earlier scope matched by company name. For multinational corporate groups sharing identical names, logic was refined to cross-validate country/city tokens (ZIZURKIL / ASTEASU) and CMIR material mapping. Replaced unformatted text conversion with direct Graph API .msg extraction."
    ),
    (
        26, "Order 1536356",
        "Order created with wrong Ship-to party (4020041562 vs 4020041584). PO mentioned 'Bitte liefern an LPD Betrieb Covestro Deutschland AG Chempark, Geb. B 768'. Email only had PO attached, no .msg email.",
        "Agent Refinement (New Requirement: Industrial Park Building-Level Address Disambiguation)",
        "Fixed",
        "Scope refinement: Original specification resolved Ship-to by Customer Name + City + Postal Code. For large enterprise chemical complexes (Chempark Dormagen) where multiple Ship-tos share identical city and postal code, logic was refined to extract and heavily weight building numbers ('Geb. B 768') and plant identifiers ('LPD Betrieb')."
    ),
    (
        34, "Christian 5",
        "No material description on PO, only in email text and CMIR + separate document attached. Exception PO was only sent to Touchless Order mailbox and archived in subfolder 'Exception POs' without emailing CSR ER.",
        "Process & Exception Routing Refinement (3-Tier CSR Architecture)",
        "Fixed",
        "Workflow refinement: Original pipeline archived unresolvable POs to the mailbox folder. Refined to implement 3-Tier active escalation: Customer ER -> Sales Org Mailbox (CUSTOMERCARE-EU@ENVALIOR.COM) -> Regional fallback, ensuring CSRs receive active notifications."
    ),
    (
        37, "Christian 8 (SO 1536335 & 1536338)",
        "2 POs in 1 email (133.PDF & 155.PDF). Only one email sent to SO Archive Robot mailbox (for SO 1536335) + wrong Ship-to party selected (4020000301 vs 4020040559).",
        "Agent Refinement (Multi-PO Looping & Italian Street-Level Token Matching)",
        "Fixed",
        "Notification refinement: Pipeline was refined to trigger separate Roborana archive emails for each generated SO number when multiple POs exist in one email. Also refined Italian address ranker to inspect street names ('Via del Padule') when postal code and city match."
    ),
    (
        61, "SOs 1536513–1536519",
        "No email in SO Archive Robot mailbox + all 6 orders created with Sold-to address (4020044282) instead of delivery address (4020044845 Catoira).",
        "Agent Refinement (Multi-Lingual Spanish Delivery Keyword Expansion)",
        "Fixed",
        "Language expansion refinement: Added recognition of Spanish delivery phrases ('Dirección de Entrega', 'PORTAL') to prioritize destination address blocks over header Sold-to addresses."
    ),
    (
        62, "Roborana Archive Inconsistencies",
        "Archive emails missing attachments, unreadable .msg files, 'Robona' typo in template body, and truncated order numbers in subject lines.",
        "Process & MIME Standard Refinement (Outlook .msg MIME Override & Branding)",
        "Fixed",
        "Technical refinement: Corporate email gateways corrupt .msg files sent with default octet-stream MIME type. Enforced explicit 'application/vnd.ms-outlook' MIME mapping. Corrected vendor branding to 'Roborana' and optimized subject line layout."
    ),
    (
        63, "Christian 19",
        "PO in German language, Z01 activated in Customer Group 2. No order created and no exception email received either.",
        "Process Refinement (Zero-Drop Exception Routing Gate)",
        "Fixed",
        "Reliability refinement: Closed all silent pipeline exit points. Any order blocked by pre-flight business rules (e.g. Z01 customer conditions) is guaranteed to dispatch an explanatory exception email to CSR."
    ),
    (
        64, "PO 1536505",
        "Completely wrong PO date: PO date extracted as 20.07.2021 instead of 21.07.2026. Document mentioned 'Data documento 21/07/2026' (Italian).",
        "Agent Refinement (Multi-Lingual Italian Date Vocabulary)",
        "Fixed",
        "Vocabulary refinement: Added Italian document phrase 'Data documento' to primary date dictionary and enforced 4-digit year prioritization over historical metadata."
    ),
    (
        65, "PO 1556600",
        "Completely wrong PO date: PO date extracted as 20.07.2021 instead of 21.07.2026. Document mentioned 'Date: 21-07-2026'.",
        "Agent Refinement (Hyphenated Date Delimiter Normalization)",
        "Fixed",
        "Regex refinement: Standardized date normalizer to handle hyphenated dates ('DD-MM-YYYY') identically to slash/dot dates, formatting cleanly for SAP."
    ),
    (
        66, "PO 1536498",
        "Completely wrong PO date: PO date extracted as 20.07.2022 instead of 22.07.2026. Document mentioned 'Bestelldatum: 22.07.26'.",
        "Agent Refinement (German Date Phrasing & 2-Digit Year Century Rule)",
        "Fixed",
        "Century logic refinement: Added German keyword 'Bestelldatum' and implemented a strict century pivot rule converting 2-digit '26' to current century '2026'."
    ),
    (
        67, "POs 1536504 + 1536518",
        "No email in SO Archive Robot mailbox + duplicated orders + wrong Ship-to parties + wrong PO date (20.07.2023 vs 23.07.2026).",
        "Action Flow & Pipeline Refinement (Idempotency Hash Deduplication)",
        "Fixed",
        "Infrastructure refinement: Added MD5 content hash deduplication ('robona_hash' = mailbox|SO|PO) in Azure Table Storage, preventing Action Flow retries from creating duplicate SAP orders. Fixed European dot-date parsing."
    ),
    (
        68, "Christian 13",
        "Dates in format DDMMYY, quantities in US format. No order created, but no exception email received either.",
        "Agent Refinement (Dense Date Format & US Numeric Delimiters)",
        "Fixed",
        "Format expansion refinement: Added support for unpunctuated compact date strings ('DDMMYY' / 'YYYYMMDD') and US-style quantity thousands-separators."
    ),
    (
        69, "PO 1536512",
        "Completely wrong PO date: PO date extracted as 20.07.2024 instead of 24.07.2026. Document mentioned 'PO number/date 24.07.2026'.",
        "Agent Refinement (Compound Header Label Tokenizer)",
        "Fixed",
        "Parsing refinement: Deployed compound header splitter to separate joint labels (e.g. 'PO number/date') into independent PO number and Date tokens."
    ),
    (
        76, "Christian 25 (SO 1536555 & 1536530)",
        "No attachment (details in email body). Two orders created for completely different customer (4020001301) and material with date in 2004.",
        "Agent Refinement (Removal of Legacy Historical Database Fallback)",
        "Fixed",
        "Safety refinement: Removed legacy fuzzy fallback query to historical sheet3 data. Deployed strict confidence gate requiring >=85% certainty before processing; completely blocks order creation when no valid PO document is present."
    ),
    (
        77, "Christian 24 (SO 1536702)",
        "Wrong PO date: Order created with PO date 20.02.2026 instead of 26.02.2026.",
        "Agent Refinement (Dual-Engine Multi-Pass OCR Disambiguation)",
        "Fixed",
        "OCR refinement: Implemented dual-pass OCR (PaddleOCR text + Vision LLM contextual inspection) to disambiguate compressed or degraded numerals ('26' vs '20')."
    ),
    (
        78, "Christian 23",
        "Italian PO + customer exists in 3 Sales orgs (2500, 2540, 2545) + GCOP document attached. No order created and no exception email received.",
        "Envalior Master Data Setup / Disambiguation Refinement",
        "Fixed",
        "Master data disambiguation: Customer existed across 3 sales orgs but CMIR was only populated in 2545. Logic was refined to cross-reference CMIR availability across sales orgs to pick the only valid active operating unit."
    ),
    (
        79, "Christian 22",
        "German PO with deviating Ship-to party. No order created and no exception email received either.",
        "Agent Refinement (Decoupled Delivery Address Evaluation)",
        "Fixed",
        "Logic refinement: Decoupled Ship-to address extraction from billing address so that differing postal codes in German multi-location accounts trigger independent Ship-to resolution."
    ),
    (
        80, "Christian 21 (SO 1536697)",
        "Completely wrong PO date: Document mentioned 'DATA ORDINE 15.10.2025', but order created with PO date 20.10.2015.",
        "Agent Refinement (Header Date Priority over Footnote Revisions)",
        "Fixed",
        "Hierarchy refinement: Added Italian token 'DATA ORDINE' and enforced strict priority of header order dates over secondary footer/revision dates."
    )
]

for row_idx, row_data in enumerate(data2, start=5):
    for col_idx, val in enumerate(row_data, start=1):
        cell = ws2.cell(row=row_idx, column=col_idx, value=val)
        cell.font = data_font
        cell.border = thin_border
        
        if col_idx == 1:
            cell.alignment = Alignment(horizontal="center", vertical="center")
        elif col_idx == 5:
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.fill = pass_fill
            cell.font = pass_font
        else:
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

# ═════════════════════════════════════════════════════════════════════════════
# AUTO-FIT COLUMN WIDTHS & SET ROW HEIGHTS
# ═════════════════════════════════════════════════════════════════════════════
for ws, col_widths in [
    (ws1, {1: 6, 2: 32, 3: 30, 4: 15, 5: 20, 6: 16, 7: 18, 8: 16, 9: 26, 10: 55}),
    (ws2, {1: 10, 2: 24, 3: 45, 4: 35, 5: 16, 6: 55})
]:
    ws.row_dimensions[4].height = 28
    for col_idx, width in col_widths.items():
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = width

output_path = "UAT_Order_Processing_and_Defect_Analysis.xlsx"
wb.save(output_path)
print(f"[OK] Successfully created Excel report: {os.path.abspath(output_path)}")
