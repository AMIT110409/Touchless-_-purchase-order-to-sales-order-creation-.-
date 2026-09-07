# 📋 Touchless Order Creation — Backlog & Sprint Work Items

This document tracks completed work items, sprint backlogs, and verification tasks for the Touchless Order Creation Pipeline.

---

## 🚀 Completed Work Items (Sprint Ending June 12, 2026)

### 🎫 Item-101: Outlook `.msg` Ingestion & Preprocessing
* **Description**: Enable the pipeline to ingest and parse Outlook email files (`*.msg`) directly, extracting their text bodies and embedded attachments so they can be processed by OCR/LLM like standard PDFs/images.
* **Status**: ✅ **Done**
* **Technical Details**:
  - Implemented `.msg` file identification in `outlook_poller.py`.
  - Added dependency and pre-processing logic in `po_extraction_enhanced.py` using `extract-msg`.
  - Extracted body text to `*_body.txt` and attachments to `*_att_*` before passing them to the main extraction pipeline.

### 🎫 Item-102: Multi-Page OCR Support for Scanned PDFs
* **Description**: Fix the "Empty OCR" issue on multi-page scanned documents where only page 1 was processed and page 2+ content was skipped.
* **Status**: ✅ **Done**
* **Technical Details**:
  - Modified `run_ocr_tool.py` to loop through all page results returned by PaddleOCR.
  - Merged text lines across all pages chronologically.

### 🎫 Item-103: CPU Stability & C++ Silent Crash Resolution
* **Description**: Resolve the silent crash (exit code 1) occurring on CPU-based environments during PaddleOCR execution.
* **Status**: ✅ **Done**
* **Technical Details**:
  - Purged local pip cache and freed 4.91 GB of disk space on `C:`.
  - Downgraded `numpy` to `1.26.4` and `paddlepaddle` to `3.2.2` to resolve the binary compatibility segfault.

### 🎫 Item-104: Smart Customer Disambiguation (Material Overlap & Address Scoring)
* **Description**: Implement multi-step disambiguation to pick the correct customer ID when fuzzy customer names match multiple candidate entries.
* **Status**: ✅ **Done**
* **Technical Details**:
  - **Step 1**: Match by material code overlap (extracted items vs customer materials database).
  - **Step 2**: Match by city/country address scoring (extracted address details compared to master database records). Country match = +2 points, City match = +1 point.

### 🎫 Item-105: Customer Group 2 Split Rules Correction
* **Description**: Correct the reversed mapping rules for customer group 2 to align with business requirements.
* **Status**: ✅ **Done**
* **Technical Details**:
  - Configured `Z01` $\rightarrow$ Separate Sales Orders per line item (Rule `'O'`).
  - Configured `'-'` (or empty) $\rightarrow$ One combined Sales Order per PO (Rule `'X'`).
  - Verified logic using automated unit tests.

### 🎫 Item-106: Email Exception File Attachments
* **Description**: Fix the bug in Stage 1 Exception routing where notifications were sent to CSR mailboxes without the original PO document attached.
* **Status**: ✅ **Done**
* **Technical Details**:
  - Corrected list-passing TypeError in `graph_email_handler.py` (allowing standard strings, path objects, or lists of paths).
  - Ensured original PDF and MSG files are retrieved from `outlook_po_extracted` or `outlook_po_archive` recursively and attached.

### 🎫 Item-107: Stage 2 Semicolon-Separated SO Numbers
* **Description**: Group Celonis feedback results by PO number and merge all generated Sales Order numbers into a single semicolon-separated list for Robona notification.
* **Status**: ✅ **Done**
* **Technical Details**:
  - Modified `run_celonis_feedback.py` to group feedback rows by `PO_NUMBER`.
  - Merged `SO_NUMBER` values into a semicolon-separated string (e.g. `1001; 1002`) so the RPA tool links the email thread to all generated SAP orders.

---

## 📅 Historical 5-Day Sprint Timeline (Setup & Integration)

### Day 1: Project Setup & Database Tracking
* **Key Tasks**: Setup Microsoft Graph API credentials; initialize `processed_emails.db`; handle cp1252 charmap encoding issues on Windows.
* **Status**: ✅ **Completed**

### Day 2: Order Mapping Logic & Grouping Rules
* **Key Tasks**: Cache Celonis master data; build decision tree classifier for order types (TA vs KB).
* **Status**: ✅ **Completed**

### Day 3: Graph API Ingestion & Archiving Handler
* **Key Tasks**: Graph API MSAL auth integration; write Outlook polling loop; setup mailbox archiving.
* **Status**: ✅ **Completed**

### Day 4: Exception Routing & Attachment Resolving
* **Key Tasks**: Map Sales Orgs to CS mailboxes; build Graph email attachment payloads; handle directory walking.
* **Status**: ✅ **Completed**

### Day 5: End-to-End (E2E) Testing & Validation
* **Key Tasks**: Execute verification tests against the integrated pipeline; check regional routing.
* **Status**: ✅ **Completed**
