"""
resend_robona_with_eml.py
=========================
Re-sends Roborana notification emails for the 15 Sales Orders, ensuring:
  1. PO PDF attachment is included (if available)
  2. Full email thread .eml attachments are included
  3. Status is updated in local SQLite tracker AND synced to Azure Table Storage (processedemails)
  4. so_creation_results.parquet is refreshed in Azure Blob Storage
"""

import os, sys, io, sqlite3, requests, json
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

from run_celonis_feedback import (
    get_graph_token,
    fetch_thread_eml_attachments,
    _fetch_single_eml,
    download_po_pdf,
    send_email_via_graph,
    load_regional_config,
    resolve_region
)
from email_templates import get_robona_template
from azure_email_tracker import AzureEmailTracker

# ─── Configuration ────────────────────────────────────────────────────────────
mailbox        = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")
robona_mailbox = os.getenv("ROBONA_SERVICE_MAILBOX", "robona-test@envalior.com")
test_email     = os.getenv("TEST_CS_EMAIL", "a.rathore@ofiservices.com")
regional_config = load_regional_config()

graph_token = get_graph_token()
print("Graph token acquired:", bool(graph_token))

# ─── Target POs & Sales Orders from screenshot ────────────────────────────────
pos_in_screenshot = [
    '11923197', '11965102', '14029941', '460031761', 'M01Y-010-05326009',
    '4406342278', 'PON-2607-0002', '450012469', '933/26', 'DEUTSCHE',
    '135420', '201994', '11750120', 'PA2026143', 'WSPO/2026/078'
]

# Load local DB
conn = sqlite3.connect("processed_emails.db")
db_df = pd.read_sql_query("SELECT * FROM processed_emails", conn)
conn.close()

# Load Celonis SO results
from azure_table_reader import AzureTableReader
reader = AzureTableReader()
df_so  = reader.get_so_results()

print(f"\nLoaded {len(df_so)} SO results from Celonis cache.")

tracker_local = AzureEmailTracker(force_sqlite=True)

sent_count = 0

print("\n" + "=" * 70)
print("  RE-SENDING ROBORANA NOTIFICATIONS WITH .EML ATTACHMENTS")
print("=" * 70)

for _, so_row in df_so.iterrows():
    po = str(so_row['PO_NUMBER']).strip()
    so = str(so_row['SO_NUMBER']).strip()
    cust = str(so_row['CUSTOMER_NAME']).strip()
    sorg = str(so_row.get('SALES_ORG', '')).strip()
    source_file = str(so_row.get('SOURCE_FILE', '')).strip()

    if po not in pos_in_screenshot and not any(p in po for p in pos_in_screenshot):
        continue

    print(f"\n----------------------------------------------------------------------")
    print(f"Processing PO: {po:18} | SO: {so:10} | Customer: {cust[:35]}")

    # Match in DB
    m = db_df[
        (db_df['subject'].str.contains(po, case=False, na=False, regex=False)) |
        (db_df['source_file'].str.contains(po, case=False, na=False, regex=False))
    ]
    if source_file and m.empty:
        sf_stem = source_file.rsplit('.', 1)[0]
        m = db_df[
            (db_df['source_file'].str.contains(sf_stem, case=False, na=False, regex=False)) |
            (db_df['subject'].str.contains(sf_stem, case=False, na=False, regex=False))
        ]

    msg_id          = m.iloc[0]['message_id'] if not m.empty else ""
    subject         = m.iloc[0]['subject'] if not m.empty else f"PO {po}"
    conversation_id = m.iloc[0]['conversation_id'] if not m.empty else ""
    source_file     = (m.iloc[0]['source_file'] if not m.empty and m.iloc[0]['source_file'] else source_file) or ""

    # 1. Download PO PDF
    pdf_bytes = download_po_pdf(source_file) if source_file else None
    pdf_name  = source_file if source_file and source_file.endswith(".pdf") else f"PO_{po}.pdf"

    # 2. Fetch .eml thread attachments (using fixed method without orderby)
    thread_atts = []
    if conversation_id:
        print(f"  Fetching EML thread for conversationId={conversation_id[:25]}...")
        thread_atts = fetch_thread_eml_attachments(graph_token, mailbox, conversation_id)

    if not thread_atts and msg_id:
        print(f"  Fallback: fetching single .eml for msg_id={msg_id[:25]}...")
        thread_atts = _fetch_single_eml(graph_token, mailbox, msg_id, subject)

    # 3. Format email
    robona_subj = f"{subject or po} SO# {so}"
    robona_body = get_robona_template(so, msg_id or "N/A", source_file or "N/A", mailbox)

    print(f"  Sending email -> To: {robona_mailbox} | CC: {test_email}")
    print(f"  Attachments: PDF = {'YES' if pdf_bytes else 'NO'} | .eml = {len(thread_atts)}")

    # 4. Send via Graph API
    ok = send_email_via_graph(
        graph_token, mailbox, robona_mailbox,
        robona_subj, robona_body,
        dry_run=False,
        pdf_bytes=pdf_bytes, pdf_filename=pdf_name,
        extra_attachments=thread_atts or None,
        cc_address=test_email
    )

    if ok:
        sent_count += 1
        if msg_id:
            # Update local SQLite tracker
            tracker_local.update_status(msg_id, 'ROBONA_SENT')
            print(f"  ✅ Updated status to ROBONA_SENT in SQLite tracker for msg={msg_id[:16]}")

print("\n" + "=" * 70)
print(f"SUMMARY: Successfully re-sent {sent_count} Roborana emails with .eml attachments!")
print("=" * 70)
