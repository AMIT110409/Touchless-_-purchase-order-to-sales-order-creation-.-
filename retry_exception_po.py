"""
retry_exception_po.py
=====================
Reprocess a PO that went to the EXCEPTION folder WITHOUT asking the client to resend.

This is the correct way to retry a PO after a bug fix (e.g. ship-to mapping fix).

How it works:
  1. Finds the PO file in outlook_po_extracted/ or outlook_po_archive/
  2. Copies it into outlook_po_current_run/ (the pipeline staging folder)
  3. Resets its status in Azure Tracker to 'RETRY_PENDING'
  4. Optionally runs the full pipeline immediately (--run-pipeline)

Usage:
  python retry_exception_po.py --po-number "4510180058"
  python retry_exception_po.py --filename "GEWISS_PO_12345.pdf"
  python retry_exception_po.py --list-exceptions          # Show all exception POs
  python retry_exception_po.py --po-number "4510180058" --run-pipeline
"""

import os
import sys
import shutil
import argparse
import json
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"

SCRIPT_DIR   = Path(__file__).parent
ARCHIVE_DIR  = SCRIPT_DIR / "outlook_po_archive"
EXTRACT_DIR  = SCRIPT_DIR / "outlook_po_extracted"
STAGE_DIR    = SCRIPT_DIR / "outlook_po_current_run"
ENRICHED_OUT = "results_outlook_po_extracted_enriched.jsonl"

EXCEPTION_STATUSES = [
    "EXCEPTION",
    "MAPPING_FAILED",
    "PREFLIGHT_FAILED",
    "SO_BLOCKED",
    "EXTRACTION_FAILED",
    "AI_PROCESSING_FAILED",
]


def get_tracker():
    from azure_email_tracker import AzureEmailTracker
    return AzureEmailTracker()


def list_exceptions(tracker):
    """Print all POs currently in an exception/failed state."""
    print("\n  Querying Azure Tracker for exception POs...\n")
    rows = tracker.get_eligible_emails(EXCEPTION_STATUSES)
    if not rows:
        print("  No exception POs found in tracker.")
        return []

    print(f"  {'Status':<25}  {'PO Number':<20}  {'Source File'}")
    print(f"  {'-'*25}  {'-'*20}  {'-'*40}")
    for r in rows:
        status   = r.get("status", "?")
        po_num   = r.get("po_number", r.get("subject", "?"))
        src_file = r.get("source_file", r.get("attachments", "?"))
        print(f"  {status:<25}  {po_num:<20}  {src_file}")
    print(f"\n  Total: {len(rows)} exception PO(s)")
    return rows


def find_po_file(filename: str = "", po_number: str = ""):
    """Search for a PO file by filename or PO number in all known folders."""
    search_dirs = [STAGE_DIR, EXTRACT_DIR, ARCHIVE_DIR, SCRIPT_DIR]

    # Direct filename search
    if filename:
        fn_lower = filename.lower()
        for d in search_dirs:
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if f.is_file() and f.name.lower() == fn_lower:
                    print(f"  [Find] Found file by name: {f}")
                    return f

    # PO number search - scan enriched JSONL for source_file matching this PO
    if po_number:
        po_clean = po_number.strip()
        enriched_path = SCRIPT_DIR / ENRICHED_OUT
        if enriched_path.exists():
            print(f"  [Find] Scanning {ENRICHED_OUT} for PO '{po_clean}'...")
            with open(enriched_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        hdr_po = str(
                            (obj.get("header_fields") or {}).get("po_number", "")
                        ).strip()
                        if hdr_po == po_clean:
                            src = obj.get("source_file", "")
                            if src:
                                found = find_po_file(filename=os.path.basename(src))
                                if found:
                                    return found
                                print(f"  [Find] PO '{po_clean}' found in JSONL as '{src}' but file not on disk.")
                    except Exception:
                        pass

        # Fallback: search tracker
        print(f"  [Find] Checking Azure Tracker for PO '{po_clean}'...")
        try:
            tracker = get_tracker()
            rows = tracker.get_emails_by_po_number(po_clean)
            for r in rows:
                src = r.get("source_file") or r.get("attachments", "")
                if src:
                    fn = os.path.basename(src.split(",")[0].strip())
                    found = find_po_file(filename=fn)
                    if found:
                        return found
        except Exception as e:
            print(f"  [WARN] Tracker search failed: {e}")

    print(f"  [Find] Could not locate PO file (filename='{filename}', po_number='{po_number}')")
    return None


def reset_tracker_status(tracker, po_number: str = "", source_file: str = ""):
    """Reset the tracker status for a PO to allow reprocessing."""
    reset_count = 0
    try:
        rows_to_reset = []

        if po_number:
            rows_to_reset = tracker.get_emails_by_po_number(po_number.strip())

        if not rows_to_reset and source_file:
            rec = tracker.get_email_by_filename(source_file)
            if rec:
                rows_to_reset = [rec]

        if not rows_to_reset:
            all_exceptions = tracker.get_eligible_emails(EXCEPTION_STATUSES)
            src_lower = os.path.basename(source_file).lower() if source_file else ""
            for r in all_exceptions:
                r_src = str(r.get("source_file") or r.get("attachments", "")).lower()
                if src_lower and (src_lower in r_src or r_src in src_lower):
                    rows_to_reset.append(r)

        if not rows_to_reset:
            print(f"  [Tracker] No tracker record found for PO='{po_number}' / file='{source_file}'")
            print(f"  [Tracker] Pipeline will still process the staged file.")
            return 0

        for r in rows_to_reset:
            msg_id = r.get("message_id") or r.get("RowKey", "")
            if not msg_id:
                continue
            old_status = r.get("status", "?")
            tracker.update_status(msg_id, "RETRY_PENDING", extra_fields={
                "error_log":    "",
                "block_code":   "",
                "block_reason": f"Retrying after exception - was: {old_status}",
            })
            print(f"  [Tracker] Reset {msg_id[:20]}... : '{old_status}' -> 'RETRY_PENDING'")
            reset_count += 1

    except Exception as e:
        print(f"  [Tracker-WARN] Could not reset tracker status: {e}")

    return reset_count


def stage_file_for_retry(po_file):
    """Copy the PO file into the pipeline staging folder."""
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    dest = STAGE_DIR / po_file.name
    try:
        # If the file is already in the staging folder, nothing to do
        if po_file.resolve() == dest.resolve():
            print(f"  [Stage] File already in staging folder - no copy needed: {po_file.name}")
            return True
        shutil.copy2(po_file, dest)
        print(f"  [Stage] Copied '{po_file.name}' -> {STAGE_DIR}/")
        return True
    except shutil.SameFileError:
        print(f"  [Stage] File already in staging folder - no copy needed: {po_file.name}")
        return True
    except Exception as e:
        print(f"  [Stage-ERR] Could not copy file: {e}")
        return False


def evict_dedup_registry(po_number: str, source_file: str):
    """
    Remove a PO from the Celonis push dedup registry so it can be re-pushed.
    Registry is in: Azure Blob 'celonis-tables/pushed_po_registry.json'
    Fallback: local '.celonis_cache/pushed_po_registry.json'
    """
    import json as _json
    import logging
    logging.getLogger('azure.identity').setLevel(logging.ERROR)

    po_clean  = str(po_number).strip().lower()
    src_clean = os.path.basename(str(source_file)).lower()

    # ── Try local cache first (always present after first run) ──────────────
    local_registry_path = SCRIPT_DIR / ".celonis_cache" / "pushed_po_registry.json"
    if local_registry_path.exists():
        try:
            with open(local_registry_path, "r", encoding="utf-8") as f:
                registry = _json.load(f)
            keys_to_delete = [
                k for k in registry
                if po_clean in str(k).lower() or src_clean in str(k).lower()
            ]
            for k in keys_to_delete:
                del registry[k]
            if keys_to_delete:
                with open(local_registry_path, "w", encoding="utf-8") as f:
                    _json.dump(registry, f, indent=2)
                print(f"  [Dedup] Local cache: evicted {len(keys_to_delete)} key(s) for PO '{po_number}'")
            else:
                print(f"  [Dedup] Local cache: no keys found for '{po_number}' - nothing to evict.")
        except Exception as e:
            print(f"  [Dedup-WARN] Local cache eviction failed: {e}")

    # ── Also evict from Azure Blob using correct MD5 composite key ────────────
    # The registry key is MD5 of: PO_NUMBER|SOLD_TO_ID|INTERNAL_MATERIAL_CODE|QUANTITY|DELIVERY_DATE
    # We need to compute it from the retry CSV to evict the exact key.
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
        blob_url = os.getenv("AZURE_BLOB_URL", "")
        if not blob_url:
            print("  [Dedup] AZURE_BLOB_URL not set - skipping Azure registry eviction.")
            return

        credential = DefaultAzureCredential()
        svc = BlobServiceClient(account_url=blob_url, credential=credential)
        # CORRECT: registry lives in 'celonis-tables' container
        container = svc.get_container_client("celonis-tables")
        registry_blob = "pushed_po_registry.json"

        try:
            raw = container.download_blob(registry_blob).readall()
            registry = _json.loads(raw)
        except Exception as e:
            print(f"  [Dedup] Could not load Azure registry ({e}) - skipping Azure eviction.")
            return

        # Build the composite MD5 keys from the retry CSV (same logic as push_to_celonis.py)
        import hashlib as _hashlib
        import csv as _csv
        DEDUP_FIELDS = ["PO_NUMBER", "SOLD_TO_ID", "INTERNAL_MATERIAL_CODE", "QUANTITY", "DELIVERY_DATE"]
        retry_csv = SCRIPT_DIR / "extracted_pos_retry_run.csv"
        computed_keys = []
        if retry_csv.exists():
            with open(retry_csv, "r", encoding="utf-8", errors="replace") as cf:
                reader = _csv.DictReader(cf)
                for row in reader:
                    raw_key = "|".join(str(row.get(f, "")).strip() for f in DEDUP_FIELDS)
                    computed_keys.append(_hashlib.md5(raw_key.encode("utf-8")).hexdigest())

        # The registry JSON has structure: {"pushed_keys": {"<md5>": <timestamp>, ...}, "total": N, ...}
        # The actual MD5 hashes are INSIDE pushed_keys, NOT at the root level.
        pushed_keys_dict = registry.get("pushed_keys", {})
        if not isinstance(pushed_keys_dict, dict):
            # Fallback: some versions store hashes directly at root
            pushed_keys_dict = {k: v for k, v in registry.items()
                                if k not in ("last_updated", "total")}

        removed = []
        for k in list(pushed_keys_dict.keys()):
            if k in computed_keys:
                del pushed_keys_dict[k]
                removed.append(k)

        if removed:
            registry["pushed_keys"] = pushed_keys_dict
            registry["total"] = len(pushed_keys_dict)
            content = _json.dumps(registry, indent=2).encode("utf-8")
            container.upload_blob(registry_blob, content, overwrite=True)
            print(f"  [Dedup] Azure: evicted {len(removed)} registry key(s) for PO '{po_number}'")
            for k in removed:
                print(f"    - {k}")
        else:
            sample = list(pushed_keys_dict.keys())[:3]
            print(f"  [Dedup] Azure: no matching keys found (computed {len(computed_keys)} MD5 key(s)).")
            if computed_keys:
                print(f"  [Dedup] Computed MD5: {computed_keys[0]}")
                print(f"  [Dedup] pushed_keys sample (first 3): {sample}")
            print(f"  [Dedup] NOTE: The PO may not have been previously pushed, or the key schema changed.")


    except Exception as e:
        print(f"  [Dedup-WARN] Registry eviction failed (non-blocking): {e}")




def run_pipeline_for_file(po_file):
    """
    Run the full extraction pipeline for a single PO file.

    Instead of calling run_outlook_to_pipeline.py (which wipes the staging folder),
    this runs the individual steps directly on a dedicated retry temp folder.
    This avoids the file being deleted by the pipeline's folder-clear logic.
    """
    import subprocess
    python_exe = sys.executable

    # Create an isolated retry folder with ONLY this PO file
    retry_folder = SCRIPT_DIR / "outlook_po_retry_run"
    if retry_folder.exists():
        shutil.rmtree(retry_folder)
    retry_folder.mkdir(parents=True, exist_ok=True)

    # Copy the file into the isolated retry folder
    dest = retry_folder / po_file.name
    shutil.copy2(po_file, dest)
    print(f"  [Retry] Isolated PO file in retry folder: {dest}")

    # Output file names for this retry run
    raw_out      = str(SCRIPT_DIR / "results_retry_run.jsonl")
    enriched_out = str(SCRIPT_DIR / "results_retry_run_enriched.jsonl")
    csv_out      = str(SCRIPT_DIR / "extracted_pos_retry_run.csv")

    # Clear stale output files from any previous retry run so dedup/push
    # never picks up data belonging to a different PO.
    for _stale in [raw_out, enriched_out, csv_out]:
        try:
            if os.path.exists(_stale):
                os.remove(_stale)
                print(f"  [Retry] Cleared stale file: {os.path.basename(_stale)}")
        except Exception:
            pass

    def run_step(cmd: str, step_name: str) -> bool:
        print(f"\n  {'='*55}")
        print(f"  {step_name}")
        print(f"  {'='*55}")
        r = subprocess.run(cmd, shell=True)
        if r.returncode != 0:
            print(f"  [FAIL] {step_name} exited with code {r.returncode}")
            return False
        print(f"  [OK] {step_name}")
        return True

    # Safety guard: ensure retry folder still contains the PO file right before
    # extraction runs.  The post-processing step of a prior retry can wipe
    # outlook_po_current_run which may also clear our retry folder indirectly.
    if not dest.exists():
        print(f"  [Retry] WARNING: retry folder was cleared - re-creating and re-copying file.")
        retry_folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(po_file, dest)
        print(f"  [Retry] Re-copied '{po_file.name}' -> {retry_folder}/")

    # Step 1: Extract
    ok = run_step(
        f'"{python_exe}" po_extraction_enhanced.py --folder "{retry_folder}" --output "{raw_out}"',
        "1. PDF/OCR Extraction",
    )
    if not ok:
        return False

    # Step 2: Enrich + Map
    ok = run_step(
        f'"{python_exe}" reenrich_results.py --input "{raw_out}" --output "{enriched_out}" --use-azure-mapper',
        "2. KB Enrichment & Azure Mapping",
    )
    if not ok:
        return False

    # Step 3: CSV Export
    ok = run_step(
        f'"{python_exe}" run_test_export.py --input "{enriched_out}" --output "{csv_out}"',
        "3. CSV Export",
    )
    if not ok:
        print("  [WARN] CSV export failed - continuing anyway.")

    # Step 3.5: Evict from dedup registry so it can be re-pushed
    import json as _json, os as _os
    po_num_for_evict = ""
    try:
        with open(enriched_out, "r", encoding="utf-8", errors="replace") as ef:
            for line in ef:
                line = line.strip()
                if not line:
                    continue
                obj = _json.loads(line)
                po_num_for_evict = str(
                    (obj.get("header_fields") or {}).get("po_number", "")
                ).strip()
                break
    except Exception:
        pass

    evict_dedup_registry(po_num_for_evict or po_file.stem, po_file.name)

    # Step 3.6: Push to Celonis
    ok = run_step(
        f'"{python_exe}" push_to_celonis.py --input "{csv_out}"',
        "3.5 Push to Celonis",
    )
    if not ok:
        print("  [WARN] Celonis push failed.")

    # Step 4: Run post-processing (exception routing + folder move)
    # Use the main pipeline in skip-outlook + skip-extraction mode so only
    # the post-processing steps run (folder moves, Robona notifications, etc.)
    ok = run_step(
        f'"{python_exe}" run_outlook_to_pipeline.py --skip-outlook --skip-extraction --skip-celonis-refresh --no-celonis',
        "4. Post-Processing (folder moves, exception routing)",
    )

    # Cleanup retry folder
    try:
        shutil.rmtree(retry_folder)
    except Exception:
        pass

    # Step 5: Update tracker status + move email in Outlook
    if po_num_for_evict:
        update_tracker_after_push(po_file.name, po_num_for_evict)

    print(f"\n  [Pipeline] Retry run complete for '{po_file.name}'")
    return True


def update_tracker_after_push(source_file: str, po_number: str):
    """
    After a successful retry push to Celonis:
      1. Update Azure Tracker status -> PUSHED_TO_CELONIS
         (Step 7 in run_outlook_to_pipeline will then move the email to 'Processed POs')
      2. Directly move the email in Outlook: Exception POs -> Processed POs
         using setup_mailbox_folders.mark_as_processed (same as the main pipeline).
    """
    tracker = get_tracker()
    src_lower = os.path.basename(source_file).lower()
    msg_id    = None
    old_status = ""

    # Find the tracker record by filename or PO number
    try:
        rec = tracker.get_email_by_filename(src_lower)
        if not rec and po_number:
            rows = tracker.get_emails_by_po_number(po_number)
            rec  = rows[0] if rows else None

        if rec:
            msg_id     = rec.get("message_id") or rec.get("RowKey", "")
            old_status = rec.get("status", "")
            # PUSHED_TO_CELONIS is what Step 7 checks to move email -> Processed POs
            tracker.update_status(msg_id, "PUSHED_TO_CELONIS", extra_fields={
                "po_number":    po_number.lower(),
                "block_code":   "",
                "error_log":    "",
                "block_reason": "Retry successful - pushed to Celonis",
            })
            print(f"  [Tracker] Updated: '{old_status}' -> 'PUSHED_TO_CELONIS' for {src_lower}")
        else:
            print(f"  [Tracker] No record found for '{src_lower}' - skipping status update.")
            return
    except Exception as e:
        print(f"  [Tracker-WARN] Status update failed: {e}")
        return

    # Directly move the email in Outlook: Exception POs -> Processed POs
    if not msg_id:
        return
    try:
        from setup_mailbox_folders import get_token, get_folder_ids, move_email_to_folder, get_messages_in_folder
        token = get_token()
        folders = get_folder_ids(token)
        processed_folder_id = folders.get("processed", "")
        exception_folder_id = folders.get("exception", "")

        # First try with the stored message ID
        ok = move_email_to_folder(token, msg_id, processed_folder_id, silent=True)
        if ok:
            tracker.update_status(msg_id, "FOLDER_MOVED", extra_fields={
                "block_reason": "Retry successful - moved from Exception POs to Processed POs"
            })
            print(f"  [Outlook] [OK] Email moved: Exception POs -> Processed POs  ({msg_id[:20]}...)")
            return

        # Stored ID is stale (404) - Exchange changes message IDs when emails are moved.
        # Search the Exception POs folder for the email by PO number / subject.
        print(f"  [Outlook] Stored ID is stale (404) - searching Exception POs for PO '{po_number}'...")
        current_msg_id = None
        try:
            msgs = get_messages_in_folder(token, exception_folder_id, top=200)
            rec_subj = str(rec.get("subject", "") or "").strip().lower()
            po_search = str(po_number).strip().lower()
            src_search = os.path.splitext(os.path.basename(source_file))[0].lower()
            # Build token sets for fuzzy matching
            src_tokens = set(re.findall(r'[a-zA-Z0-9]+', src_search)) - {'pdf', 'xlsx', 'xls', 'txt', 'excel'}
            rec_tokens = set(re.findall(r'[a-zA-Z0-9]+', rec_subj))
            po_tokens  = set(re.findall(r'[a-zA-Z0-9]+', po_search))

            for m in msgs:
                subj = str(m.get("subject", "") or "").lower()
                subj_tokens = set(re.findall(r'[a-zA-Z0-9]+', subj))

                # Exact substring match
                match_found = (
                    (len(rec_subj) >= 3 and rec_subj in subj) or
                    (len(po_search) >= 3 and po_search in subj) or
                    (len(src_search) >= 3 and src_search in subj)
                )

                # Token overlap match (e.g., if 'asos' and 'scenario' both match)
                if not match_found and len(subj_tokens) >= 2:
                    if len(src_tokens & subj_tokens) >= 2 or len(rec_tokens & subj_tokens) >= 2:
                        match_found = True

                if match_found:
                    current_msg_id = m["id"]
                    print(f"  [Outlook] Found current message ID in Exception POs: {current_msg_id[:30]}...")
                    safe_sub_print = str(m.get('subject','?'))[:60].encode('ascii', 'replace').decode('ascii')
                    print(f"  [Outlook]   subject: {safe_sub_print}")
                    break
        except Exception as se:
            print(f"  [Outlook] Could not search Exception POs: {se}")

        if current_msg_id:
            ok2 = move_email_to_folder(token, current_msg_id, processed_folder_id, silent=False)
            if ok2:
                # Update tracker with the new (current) message ID
                tracker.update_status(current_msg_id, "FOLDER_MOVED", extra_fields={
                    "block_reason": "Retry successful - moved from Exception POs to Processed POs (ID refreshed)"
                })
                # Also mark old stale record as done
                tracker.update_status(msg_id, "FOLDER_MOVED")
                print(f"  [Outlook] [OK] Email moved: Exception POs -> Processed POs (ID refreshed)")
            else:
                tracker.update_status(msg_id, "PUSHED_TO_CELONIS")
                print(f"  [Outlook] Move failed even with refreshed ID - manual action needed.")
        else:
            tracker.update_status(msg_id, "PUSHED_TO_CELONIS")
            print(f"  [Outlook] Email not found in Exception POs folder.")
            print(f"  [Outlook] It may have already been moved to Processed POs manually.")
            print(f"  [Outlook] Tracker is set to PUSHED_TO_CELONIS - Celonis Action Flow will handle SO creation.")

    except Exception as e:
        import traceback as _tb
        print(f"  [Outlook-WARN] Folder move error: {e}")
        _tb.print_exc()







def main():
    parser = argparse.ArgumentParser(
        description="Retry an exception PO without asking the client to resend."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--po-number",      type=str, help="PO number to retry (e.g. 4510180058)")
    group.add_argument("--filename",       type=str, help="Exact PO filename (e.g. GEWISS_PO.pdf)")
    group.add_argument("--list-exceptions", action="store_true",
                       help="List all exception POs currently in the tracker")

    parser.add_argument("--run-pipeline", action="store_true",
                        help="Automatically run the pipeline after staging the file")
    parser.add_argument("--no-tracker-reset", action="store_true",
                        help="Skip resetting the Azure Tracker status")
    args = parser.parse_args()

    if not any([args.po_number, args.filename, args.list_exceptions]):
        parser.print_help()
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  PO Exception Retry Tool")
    print("=" * 60)

    tracker = get_tracker()

    if args.list_exceptions:
        list_exceptions(tracker)
        return

    po_file = find_po_file(
        filename=args.filename or "",
        po_number=args.po_number or "",
    )

    if not po_file:
        print(f"\n  ERROR: Could not find the PO file. Check:")
        print(f"     - Is it in outlook_po_extracted/ or outlook_po_archive/?")
        print(f"     - Try: --filename <exact_filename.pdf>")
        sys.exit(1)

    print(f"\n  Found PO file: {po_file}")

    if not args.no_tracker_reset:
        reset_tracker_status(
            tracker,
            po_number=args.po_number or "",
            source_file=po_file.name,
        )

    staged = stage_file_for_retry(po_file)
    if not staged:
        sys.exit(1)

    print(f"\n  File staged: {STAGE_DIR / po_file.name}")

    if args.run_pipeline:
        success = run_pipeline_for_file(po_file)   # pass Path object, not string
        if not success:
            sys.exit(1)
    else:
        print(f"\n  Next step - run the pipeline manually:")
        print(f"     python run_outlook_to_pipeline.py --skip-outlook --skip-celonis-refresh")
        print(f"\n  Or run automatically with:")
        print(f"     python retry_exception_po.py --po-number \"{args.po_number or po_file.stem}\" --run-pipeline")

    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()
