<div align="center">

<h1>🤖 Touchless PO → Sales Order Creation</h1>

<p>
  <strong>An end-to-end AI agent pipeline that reads purchase order emails from Outlook, extracts structured data using OCR + LLM, validates against customer master data from Celonis, and creates SAP Sales Orders — fully automated.</strong>
</p>

<p>
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/LLM-Claude%20(Anthropic)-blueviolet?logo=anthropic" alt="Claude">
  <img src="https://img.shields.io/badge/OCR-PaddleOCR%20%2B%20PyMuPDF-orange" alt="OCR">
  <img src="https://img.shields.io/badge/Platform-Azure%20%2B%20Celonis%20%2B%20SAP-0078d4?logo=microsoftazure" alt="Azure">
  <img src="https://img.shields.io/badge/Status-UAT%20Complete-success" alt="Status">
</p>

</div>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Architecture](#-architecture)
- [Pipeline Stages](#-pipeline-stages)
- [Key Modules](#-key-modules)
- [Configuration](#-configuration)
- [Entry Points (How to Run)](#-entry-points-how-to-run)
- [Dependencies](#-dependencies)
- [Regional Routing](#-regional-routing)
- [Status Tracker Flow](#-status-tracker-flow)
- [UAT Results](#-uat-results)
- [Project Structure](#-project-structure)

---

## 🌐 Overview

This system automates the processing of incoming **Purchase Order (PO) emails** received at `salesorders@envalior.com`. Instead of a Customer Service Representative (CSR) manually reading each PO and creating a Sales Order in SAP, this pipeline:

1. **Reads emails** from the Outlook mailbox via Microsoft Graph API
2. **Extracts PO data** from PDF attachments using PaddleOCR + Claude Vision LLM (English, German, Italian, Spanish, French)
3. **Validates** the extracted data against Envalior's customer master (CMIR) and historical order mapping from Celonis
4. **Pushes** valid POs into the Celonis Action Flow, which calls the SAP BAPI (`BAPI_SALESORDER_CREATEFROMDAT2`) to create the Sales Order
5. **Dispatches confirmations** — sending the original email + PO PDF to the Roborana SO Archive Robot
6. **Routes exceptions** — any PO that cannot be processed automatically is forwarded to the correct regional Customer Service Representative

---

## 🏛️ Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         STAGE 1 — INGESTION & EXTRACTION                │
│                                                                         │
│  Outlook Mailbox                                                        │
│  (salesorders@)     ──→  Microsoft Graph API  ──→  Azure Blob Storage  │
│                                                     (input-po container)│
│                                │                                        │
│                                ▼                                        │
│                     PO Extraction Engine                                │
│                     ┌─────────────────────────┐                        │
│                     │  PyMuPDF  (text layer)  │                        │
│                     │  PaddleOCR (image scan)  │                        │
│                     │  Claude Vision (LLM)     │                        │
│                     └─────────────┬───────────┘                        │
│                                   ▼                                    │
│                         Structured JSON Output                          │
│                    (PO#, Customer, Materials, Qty,                      │
│                     Delivery Date, Ship-To, Sold-To)                   │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────────┐
│                    STAGE 1b — ENRICHMENT & VALIDATION                   │
│                                                                         │
│   Celonis CMIR / Customer Master  ◄──── Azure Blob (Parquet)           │
│   Historical Order Mapping        ◄──── Azure Blob (Parquet)           │
│                                                                         │
│   sales_order_mapper.py                                                 │
│   ┌────────────────────────────────────────────────────────┐           │
│   │  Customer match (fuzzy + CMIR)  →  Sold-To ID          │           │
│   │  Ship-To resolution (address scoring)                  │           │
│   │  Material mapping (CMIR / description fallback)        │           │
│   │  Unit normalisation (KG, MT, LBS → SAP codes)         │           │
│   │  Order type determination (NB, ZNB, ZNOR …)           │           │
│   └──────────────────────────┬─────────────────────────────┘           │
│                              ▼                                          │
│                      preflight_check.py                                 │
│                   (confidence gates, field validation)                  │
│                              │                                          │
│              ┌───────────────┴───────────────┐                         │
│              ▼ PASS                          ▼ FAIL                    │
│        push_to_celonis.py           csr_routing.py                     │
│       (Azure Table tracker)         exception_resolver.py              │
└────────────────────────────────────────────────────────────────────────┘
                 │ PASS
┌────────────────▼────────────────────────────────────────────────────────┐
│                    STAGE 2 — CELONIS ACTION FLOW                        │
│                                                                         │
│   Celonis reads the o_custom_PoExtractionResults view                  │
│   → Triggers Action Flow                                                │
│   → Calls SAP BAPI_SALESORDER_CREATEFROMDAT2                           │
│   → Writes SO_NUMBER back to Celonis view                              │
│                                                                         │
│   (typically runs 30–60 min after Stage 1)                             │
└────────────────────────────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────────────┐
│                    STAGE 2 FEEDBACK — run_celonis_feedback.py           │
│                                                                         │
│   Reads Celonis SO results  →  Matches back to original emails          │
│                                                                         │
│              ┌────────────────────┬──────────────────────┐             │
│              ▼ SO Created         ▼ SO Blocked           ▼ Failed      │
│      Roborana notification   CSR Alert Email       Exception Email      │
│   (PO PDF + .msg attached)   (block code reason)   (BAPI error reason) │
│   Azure Table: ROBONA_SENT   SO_BLOCKED             SO_CREATION_FAILED  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🔄 Pipeline Stages

### Stage 1 — Email Ingestion & PO Extraction
**Entry point:** `run_outlook_to_pipeline.py`

| Step | What Happens |
|------|-------------|
| **Outlook Pull** | Graph API fetches unread PO emails from `salesorders@envalior.com`. PDF attachments are saved to `outlook_po_extracted/` and uploaded to Azure Blob `input-po`. |
| **OCR + LLM Extraction** | `po_extraction_enhanced.py` runs PyMuPDF (native text), PaddleOCR (scanned PDFs), and Claude Vision (LLM reasoning) to produce a structured JSON per PO. |
| **Enrichment** | `reenrich_results.py` → `sales_order_mapper.py` maps customer name → Sold-To ID, resolves Ship-To party, maps materials via CMIR from Celonis parquet files on Azure Blob. |
| **Preflight** | `preflight_check.py` gates each PO: confidence ≥ 0.70, Ship-To resolved, materials mapped, quantities valid. |
| **Push or Exception** | PASS → `push_to_celonis.py` writes to Celonis. FAIL → `csr_routing.py` routes exception email to correct regional CSR inbox. |
| **Tracking** | `azure_email_tracker.py` records every email/PO in Azure Table Storage (`processedemails`). Status transitions: `PENDING` → `PUSHED_TO_CELONIS` → … |

### Stage 2 — Celonis Action Flow (SAP)
Runs automatically in Celonis after Stage 1. Calls SAP BAPI to create the Sales Order. Writes the `SO_NUMBER` back to the Celonis view.

### Stage 2 Feedback — Celonis → Notifications
**Entry point:** `run_celonis_feedback.py --current-run`

Reads Celonis SO results, matches back to original emails, dispatches Roborana archive emails (PO PDF + `.msg`) for successes, and CSR exception emails for failures.

---

## 📦 Key Modules

| Module | Role |
|--------|------|
| `run_outlook_to_pipeline.py` | **Main entry point.** Orchestrates the full Stage 1 pipeline end-to-end. |
| `run_celonis_feedback.py` | **Stage 2 feedback.** Reads Celonis SO results, routes Roborana + exception notifications. |
| `celonis_to_azure.py` | Extracts Celonis CMIR/Sheet3 tables and uploads them to Azure Blob as Parquet. Run this when Celonis data changes. |
| `po_extraction_enhanced.py` | Core OCR + LLM extraction engine. Dual-engine: PaddleOCR + Claude Vision. 2000+ lines covering all edge cases. |
| `sales_order_mapper.py` | Customer/material mapping engine. Resolves Sold-To, Ship-To, material codes, order types, UoM normalisation. |
| `enrich_results.py` | Legacy enrichment using local SQLite (knowledge_base.db). |
| `reenrich_results.py` | Production enrichment using Azure Blob Parquet (Celonis CMIR data). |
| `push_to_celonis.py` | Pushes validated PO records to Celonis via PyCelonis API. Includes MD5 deduplication registry. |
| `graph_email_handler.py` | Microsoft Graph API wrapper: archive emails, send exception emails, send Roborana notifications with PDF + `.msg` attachments. |
| `azure_email_tracker.py` | Azure Table Storage-backed email processing tracker (replaces local SQLite). |
| `csr_routing.py` | 3-Tier CSR exception email routing (CSR ER → Regional Mailbox → All Regions fallback). |
| `exception_resolver.py` | Batch re-processing tool for stuck/failed POs. Resets to PENDING for re-try. |
| `audit_logger.py` | Structured JSONL audit trail per PO, written to Azure Blob (`audit_logs/`). |
| `preflight_check.py` | Pre-push validation gate. Checks confidence, Ship-To, materials, quantities. |
| `pre_creation_validator.py` | Final SAP field validation (date formats, UoM codes, Sales Org). |
| `email_templates.py` | All HTML email templates (exception, Roborana notification, SO blocked, audit digest). |
| `order_type_decision_tree.py` | Intelligent SAP order type determination (NB, ZNB, ZNOR, etc.) based on customer + material rules. |
| `robona_so_tracker.py` | Tracks which SO → Roborana emails have already been sent (prevents duplicates). |
| `claude_client.py` | Anthropic Claude API wrapper (used by extraction engine for LLM calls). |
| `regional_config.json` | Sales Org → Regional CS email mapping for 7 regions (EU, EU05, AMS, CN_GC, JP, KR, IN). |

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and fill in all values:

```bash
cp .env.example .env
```

### Required Variables

| Variable | Description |
|----------|-------------|
| `MS_GRAPH_CLIENT_ID` | Azure App Registration Client ID |
| `MS_GRAPH_TENANT_ID` | Azure Tenant ID (Envalior) |
| `MS_GRAPH_CLIENT_SECRET` | Azure App Client Secret |
| `TARGET_EMAIL_USER` | Monitored mailbox (e.g. `salesorders@envalior.com`) |
| `AZURE_BLOB_URL` | Azure Blob Storage endpoint |
| `AZURE_STORAGE_ACCOUNT_NAME` | Storage account name |
| `AZURE_STORAGE_ACCOUNT_KEY` | Storage account key |
| `ANTHROPIC_API_KEY` | Claude API key (for LLM extraction) |
| `CELONIS_URL` | Celonis tenant URL |
| `CELONIS_API_TOKEN` | Celonis API bearer token |
| `CELONIS_POOL_ID` | Celonis Data Pool ID |
| `CELONIS_DATA_MODEL_ID` | Celonis Data Model ID |
| `SO_RESULTS_TABLE` | Celonis view name for SO creation results |
| `ROBONA_SERVICE_MAILBOX` | Roborana SO Archive Robot mailbox |
| `DEFAULT_CS_EMAIL` | Fallback CS email if routing fails |

---

## 🚀 How to Run (Full Pipeline)

> All commands use the **new production structure** (`src/pipeline/`).
> You can also use the `Makefile` shortcuts shown alongside each command.

---

### ⚙️ Prerequisites

```bash
# 1. Install base dependencies
pip install -r requirements.txt

# 2. (Optional) GPU support for local Qwen/LLaMA inference — skip if using Claude API
pip install -r requirements-gpu.txt

# 3. Azure CLI login (for Blob Storage + Table Storage access)
az login

# 4. Copy and fill in your secrets
cp .env.example .env
# → Edit .env with your MS_GRAPH_CLIENT_ID, ANTHROPIC_API_KEY, CELONIS_API_TOKEN, etc.
```

---

### 🔄 Step 0 — One-Time Setup (first run only)

```bash
# Create Azure Blob containers (input-po, celonis-tables)
powershell setup/setup_celonis_blob_container.ps1

# Create Outlook mailbox folders (Archive, Exception POs, etc.)
python setup/setup_mailbox_folders.py

# Initialize Azure Table Storage email tracker
python setup/setup_email_tracker.py
```

---

### 📥 Step 1 — Sync Celonis Master Data → Azure Blob

Run this whenever Celonis CMIR / customer master data changes.

```bash
# Full sync (CMIR customer master + historical order mapping)
python src/pipeline/celonis_to_azure.py

# Makefile shortcut:
make sync
```

---

### 🚀 Step 2 — Run the Main Pipeline (Stage 1: Email → Extraction → Celonis)

This is the **main daily command**. It:
- Reads unread PO emails from Outlook (`salesorders@envalior.com`)
- Extracts PO data via PaddleOCR + Claude Vision
- Maps customer / material / Ship-To from Celonis CMIR
- Validates via preflight check
- Pushes valid POs to Celonis Action Flow
- Routes exceptions to regional CSR inboxes

```bash
# ── Normal run (unread emails only) ─────────────────────────
python src/pipeline/run_outlook_to_pipeline.py

# Makefile shortcut:
make run

# ── Process ALL emails (not just unread) ─────────────────────
python src/pipeline/run_outlook_to_pipeline.py --all

# Makefile shortcut:
make run-all

# ── Skip Outlook pull (re-run extraction on existing folder) ──
python src/pipeline/run_outlook_to_pipeline.py --skip-outlook

# Makefile shortcut:
make skip-outlook
```

---

### ✅ Step 3 — Preflight Validation (optional review before push)

Checks every PO for readiness: confidence ≥ 0.70, Ship-To resolved, all materials mapped.

```bash
python src/pipeline/preflight_check.py

# Makefile shortcut:
make preflight

# Custom input file:
python src/pipeline/preflight_check.py --input reports/results_outlook_po_extracted_enriched.jsonl
```

---

### 📬 Step 4 — Stage 2 Feedback: Celonis SO Results → Notifications

Run this **30–60 minutes after Step 2**, once the Celonis Action Flow has processed POs and written SO numbers back.

This step:
- Reads SO creation results from Celonis
- **✅ SO Created** → Sends Roborana notification email with original PO PDF + `.msg` attached
- **⚠️ SO Blocked** → Sends CSR alert with SAP block code/reason
- **❌ SO Failed** → Sends CSR exception email with BAPI failure reason

```bash
# ── Current batch only (recommended for daily runs) ──────────
python src/pipeline/run_celonis_feedback.py --current-run

# Makefile shortcut:
make feedback

# ── Dry-run (log actions only, no API calls) ─────────────────
python src/pipeline/run_celonis_feedback.py --dry-run --current-run

# Makefile shortcut:
make feedback-dry

# ── Single PO debug ───────────────────────────────────────────
python src/pipeline/run_celonis_feedback.py --po-number PO-1234

# ── Pull only SO results from Celonis (no notifications) ──────
python src/pipeline/celonis_to_azure.py --so-results-only

# Makefile shortcut:
make so-results
```

---

### 🔁 Step 5 — Re-process Failed / Stuck POs

Resets stuck POs (status `EXCEPTION_ROUTED`, `MAPPING_FAILED`, `FOLDER_MOVED`) back to `PENDING` so the next pipeline run retries them.

```bash
# ── Dry-run first (shows what WOULD be reset — no changes) ───
python src/validation/exception_resolver.py --dry-run

# Makefile shortcut:
make exceptions-dry

# ── Live mode (resets eligible rows to PENDING) ───────────────
python src/validation/exception_resolver.py

# Makefile shortcut:
make exceptions

# ── Single customer or PO ─────────────────────────────────────
python src/validation/exception_resolver.py --po PO-1234
python src/validation/exception_resolver.py --customer "MCAM"
```

---

### 📮 Roborana — Manual Resend (if Roborana email was missed)

```bash
# Resend Roborana notification for a specific SO
python tools/resend_robona_with_eml.py

# Retry a specific failed PO through the exception handler
python tools/retry_exception_po.py

# Manually trigger exception email for a PO
python tools/send_exception_manual.py
```

---

### 📋 Quick Reference (all Makefile commands)

```bash
make help          # Show all available commands

make run           # Stage 1: process unread PO emails (daily run)
make run-all       # Stage 1: process ALL emails
make sync          # Sync Celonis CMIR → Azure Blob
make feedback      # Stage 2: send Roborana + CSR notifications
make feedback-dry  # Stage 2 dry-run (no API calls)
make so-results    # Pull SO results from Celonis only
make preflight     # Validate POs before push
make exceptions    # Re-process stuck/failed POs
```

---

### 📅 Recommended Daily Schedule

| Time | Command | What it does |
|------|---------|-------------|
| **09:00** | `make run` | Process overnight PO emails → push to Celonis |
| **10:00** | `make feedback` | Celonis Action Flow has run → send Roborana + exceptions |
| **On demand** | `make exceptions` | Re-try any POs that failed |
| **Weekly** | `make sync` | Refresh Celonis master data |

---

## 🌍 Regional Routing

Customer Service exceptions are routed to the correct regional inbox based on SAP Sales Organisation:

| Region | Sales Orgs | CS Email |
|--------|-----------|----------|
| Europe (EU) | 2500, 2540, 2561 | `CustomerCare-EU@envalior.com` |
| Europe EU05 | 2545 | `CustomerCare-EU05@envalior.com` |
| Americas | 2600, 2610, 2620, 2630 | `customerservice.dem-am@envalior.com` |
| China / Greater China | 2700, 2701, 2730, 2750, 2780 | `CSR-GC@envalior.com` |
| Japan | 2710 | `CSR-Japan@envalior.com` |
| Korea | 2760 | `CSR-KR@envalior.com` |
| India | 2720, 2721 | `CS-India.Team@envalior.com` |

**3-Tier Escalation Logic:**
- **Tier 1**: Employee Responsible (direct CSR from CMIR) → CC: Regional Mailbox
- **Tier 2**: Regional Mailbox (Sales Org → region map)
- **Tier 3**: Fuzzy address/VAT inference → All Regional Mailboxes

---

## 🔄 Status Tracker Flow

All email/PO processing state is tracked in Azure Table Storage (`processedemails`):

```
PENDING
  │
  ▼
PUSHED_TO_CELONIS          ← PO successfully sent to Celonis Action Flow
  │
  ├──▶ ROBONA_SENT          ← SO created; Roborana notification sent (touchless success)
  ├──▶ SO_BLOCKED           ← SO created but has SAP block code; CSR notified
  ├──▶ SO_CREATION_FAILED   ← BAPI RFC failed (e.g. material excluded); CSR notified
  │
EXCEPTION_ROUTED           ← Pre-flight failed; exception forwarded to CSR
MAPPING_FAILED             ← Customer or material could not be resolved
FOLDER_MOVED               ← Email archived in Outlook; awaiting Stage 2
```

---

## 📊 UAT Results

### Current Run (10 Fresh POs)

| # | PO | Customer | Extraction | Master Data | SAP Validation | SO Created | Result |
|---|----|---------|-----------|-----------|--------------|-----------|----|
| 1 | be_135796 | Brüggemann | ✓ | ✓ | ✓ | ✓ SO#1536844 | PASS |
| 2 | Bestellung Brüggemann | Brüggemann | ✓ | ✓ | ✓ | ✓ SO#1536842 | PASS |
| 3 | ex-1031059-26 | Eurochem | ✓ | ✓ | ✓ | ✓ SO#1536843 | PASS |
| 4 | Order 2600087 Durethan | Lanxess | ✓ | ✓ | ✓ | ✓ SO#1536841 | PASS |
| 5 | PO 26-312 | Stebro | ✓ | ✓ | ✓ | ✓ SO#1536839 | PASS |
| 6 | NY24409 (body-only) | — | ✓ | ✓ | — | — | EXCEPTION (no PDF) |
| 7 | 4710833926-S | BASF | ✓ | ✓ | ✓ | ✗ | EXCEPTION (Material excluded in Sales Org 2540) |
| 8 | 60680 | — | ✓ | ✓ | ✓ | ✗ | EXCEPTION (Material 0000053398 excluded) |

> **Note on exceptions:** POs 7 & 8 require Envalior to resolve the SAP material exclusion (`[E:V1:117]`) in Sales Org 2540. Once resolved, re-run:
> ```bash
> python celonis_to_azure.py --so-results-only
> python run_celonis_feedback.py --current-run
> ```

### Historical UAT (18 Defects from Christian's Previous Session)

All 18 historical defects have been **resolved**. Root causes were predominantly scope refinements:
- Multi-lingual date parsing (Italian, German, Spanish, French)
- Building-level industrial park Ship-To disambiguation
- 3-Tier CSR exception routing architecture
- MIME-standard `.msg` file attachment enforcement
- MD5 deduplication for idempotent order creation

See `UAT_Order_Processing_and_Defect_Analysis.xlsx` for the full defect analysis.

---

## 📁 Project Structure

```
│   ├── .env                          # 🔴 NOT COMMITTED — your local secrets
│   └── requirements.txt              # Python dependencies
│
├── 🛠️ SETUP & MIGRATION
│   ├── setup_mailbox_folders.py      # Create Outlook mailbox folders via Graph API
│   ├── setup_email_tracker.py        # Initialize Azure Table Storage
│   ├── setup_celonis_blob_container.ps1  # Create Azure Blob containers
│   ├── setup_azure_storage.ps1       # Azure Storage account setup
│   └── setup_outlook_app.ps1         # Azure App Registration setup
│
├── 🔧 UTILITIES & DIAGNOSTICS
│   ├── audit_output.py               # Audit log reader/formatter
│   ├── claude_client.py              # Anthropic Claude API client
│   ├── azure_worker.py               # Azure async worker utilities
│   ├── generate_mismatch_report.py   # Material mismatch reporting
│   ├── extract_customer_groups.py    # Customer group extraction from Celonis
│   ├── diagnose_*.py                 # Per-customer/PO diagnostic scripts
│   └── check_*.py                    # Validation/inspection utilities
│
└── 📄 DOCUMENTATION
    ├── README.md                      # This file
    ├── touchless-po-sap-agent-reliability-guide.md
    └── UAT_Order_Processing_and_Defect_Analysis.xlsx
```

---

## 🔐 Security

- **No credentials in source code** — all secrets are in `.env` (gitignored)
- **Azure RBAC** — Graph API uses Client Credentials flow (app-level permissions)
- **Blob Storage** — authenticated via Storage Account Key (can be upgraded to Managed Identity)
- **Audit trail** — every PO action is logged to Azure Blob (`celonis-tables/audit_logs/`)
- **Deduplication** — MD5 hash registry prevents the same PO being pushed twice

---

## 📞 Support

| Contact | Role |
|---------|------|
| `a.rathore@ofiservices.com` | Pipeline Developer / OFI Services |
| `CustomerCare-EU@envalior.com` | Envalior EU Customer Service |
| `customerservice.dem-am@envalior.com` | Envalior Americas Customer Service |

---

<div align="center">
<sub>Built for Envalior B.V. | Powered by Claude AI + PaddleOCR + Celonis + Azure</sub>
</div>
