"""
Outlook -> Storage -> PO Extraction Pipeline (One Process)
--------------------------------------------------------
Runs:
  1. Outlook extraction (all emails, body PO + attachments) -> save to folder + Azure
  2. PO extraction pipeline (po_extraction_enhanced -> reenrich -> export)

Usage:
  python run_outlook_to_pipeline.py
  python run_outlook_to_pipeline.py --all              # Process all emails (not just unread)
  python run_outlook_to_pipeline.py --skip-outlook     # Only run pipeline (use existing folder)
"""

import argparse
import subprocess
import sys
import os
import re
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Reliability additions ---------------------------------------------------
try:
    from audit_logger import get_audit_logger
except ImportError:
    def get_audit_logger():
        """Stub if audit_logger.py not found — pipeline continues without audit."""
        class _Stub:
            def log(self, **_): pass
            def flush(self): pass
        return _Stub()

try:
    from pre_creation_validator import validate_before_push
except ImportError:
    validate_before_push = None
# ---------------------------------------------------------------------------


def run_cmd(cmd: str, step_name: str) -> bool:
    """Run a shell command; return True on success, False on failure."""
    print(f"\n{'='*60}")
    print(f"  {step_name}")
    print(f"  {cmd}")
    print(f"{'='*60}")
    r = subprocess.run(cmd, shell=True)
    if r.returncode != 0:
        print(f"\n  ERROR: {step_name} failed (exit {r.returncode})")
        return False
    return True

def sync_from_blob(container_name: str, download_dir: Path) -> bool:
    blob_url = os.getenv("AZURE_BLOB_URL")
    if not blob_url:
        return False

    # Substrings that identify system-generated exception/notification files.
    # These must NEVER be downloaded back into the pipeline input folder.
    _BLOB_SKIP_SUBS = (
        'po exception',
        'ai processing failed',
        'robona-attach',
        'undeliverable',
    )

    try:
        from azure.storage.blob import BlobServiceClient
        import logging
        logging.getLogger('azure.identity').setLevel(logging.ERROR)

        account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
        if account_key:
            blob_service_client = BlobServiceClient(account_url=blob_url, credential=account_key)
        else:
            from azure.identity import DefaultAzureCredential
            credential = DefaultAzureCredential()
            blob_service_client = BlobServiceClient(account_url=blob_url, credential=credential)
        container_client = blob_service_client.get_container_client(container_name)

        if not container_client.exists():
            print(f"  -> Container '{container_name}' does not exist.")
            return False

        blobs = list(container_client.list_blobs())
        count = 0
        skipped = 0
        junk_deleted = 0
        # Also check the archive folder — Step 8 moves all processed files there.
        archive_dir = download_dir.parent / "outlook_po_archive"

        for blob in blobs:
            bname_lower = blob.name.lower()

            # ── Skip (and delete) exception/notification junk blobs ──
            if any(s in bname_lower for s in _BLOB_SKIP_SUBS):
                print(f"  [Skip+Del] Exception/junk blob removed from Azure: {blob.name[:70]}")
                try:
                    container_client.delete_blob(blob.name)
                    junk_deleted += 1
                except Exception as _de:
                    print(f"  [WARN] Could not delete junk blob '{blob.name[:50]}': {_de}")
                continue

            download_path = download_dir / blob.name
            archive_path  = archive_dir  / blob.name

            # Skip if already in active folder OR already archived (already processed)
            if download_path.exists() or archive_path.exists():
                skipped += 1
                continue

            print(f"  -> Downloading {blob.name} from Azure Blob Storage...")
            with open(download_path, "wb") as f:
                f.write(container_client.download_blob(blob.name).readall())
            count += 1

        if junk_deleted:
            print(f"  -> Removed {junk_deleted} exception/junk blob(s) from '{container_name}'.")
        if count > 0:
            print(f"  -> Downloaded {count} new file(s) from '{container_name}'.")
        else:
            print(f"  -> No new files to download ({skipped} already processed/archived).")
        return True
    except Exception as e:
        err_msg = str(e)
        if "credential" in err_msg.lower() or "token" in err_msg.lower() or "auth" in err_msg.lower():
            print(f"  -> Azure Blob auth failed (run 'az login' or check Managed Identity): {err_msg[:120]}")
        elif "name" in err_msg.lower() or "dns" in err_msg.lower():
            print(f"  -> Azure Blob URL not reachable (check AZURE_BLOB_URL in .env): {err_msg[:120]}")
        else:
            print(f"  -> Azure Blob Download skipped: {err_msg[:120]}")
        return False

def upload_to_blob(container_name: str, file_path: Path) -> bool:
    blob_url = os.getenv("AZURE_BLOB_URL")
    if not blob_url or not file_path.exists():
        return False
    try:
        from azure.storage.blob import BlobServiceClient
        import logging
        logging.getLogger('azure.identity').setLevel(logging.ERROR)
        
        account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
        if account_key:
            blob_service_client = BlobServiceClient(account_url=blob_url, credential=account_key)
        else:
            from azure.identity import DefaultAzureCredential
            credential = DefaultAzureCredential()
            blob_service_client = BlobServiceClient(account_url=blob_url, credential=credential)
        container_client = blob_service_client.get_container_client(container_name)
        
        if not container_client.exists():
            container_client.create_container()
            
        print(f"  -> Uploading {file_path.name} to Azure container '{container_name}'...")
        with open(file_path, "rb") as data:
            container_client.upload_blob(name=file_path.name, data=data, overwrite=True)
        return True
    except Exception as e:
        err_msg = str(e)
        if "credential" in err_msg.lower() or "token" in err_msg.lower() or "auth" in err_msg.lower():
            print(f"  -> Azure Blob auth failed (run 'az login' or check Managed Identity): {err_msg[:120]}")
        elif "name" in err_msg.lower() or "dns" in err_msg.lower():
            print(f"  -> Azure Blob URL not reachable (check AZURE_BLOB_URL in .env): {err_msg[:120]}")
        else:
            print(f"  -> Azure Blob Upload skipped: {err_msg[:120]}")
        return False

def get_all_message_files(msg_id: str, folder: str, db_path: Path) -> list:
    """Gather all files on disk belonging to a specific message_id (original and extracted)."""
    import sqlite3
    from typing import Optional
    files_to_attach = []
    
    if not db_path.exists():
        return []
        
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get all recorded attachments from DB for this message
        cursor.execute("SELECT filename FROM email_attachments WHERE message_id=?", (msg_id,))
        rows = cursor.fetchall()
        db_filenames = [r[0] for r in rows if r[0]]
        conn.close()
    except Exception as e:
        print(f"  [WARN] DB query error in get_all_message_files: {e}")
        db_filenames = []
        
    # Helper to find a file on disk (recursively, case-insensitive)
    def find_file(name: str) -> Optional[str]:
        # Search in active folder
        candidate = os.path.join(folder, name)
        if os.path.exists(candidate):
            return candidate
        for root, dirs, files in os.walk(folder):
            for f in files:
                if f.lower() == name.lower():
                    return os.path.join(root, f)
                    
        # Search in archive folder
        archive_folder = Path(folder).parent / "outlook_po_archive"
        if archive_folder.exists():
            candidate_archive = os.path.join(archive_folder, name)
            if os.path.exists(candidate_archive):
                return candidate_archive
            for root, dirs, files in os.walk(archive_folder):
                for f in files:
                    if f.lower() == name.lower():
                        return os.path.join(root, f)
        return None
        
    for name in db_filenames:
        fp = find_file(name)
        if fp and fp not in files_to_attach:
            files_to_attach.append(fp)
            
        # If it's a .msg file, also find any extracted components in the same folder
        if name.lower().endswith(".msg"):
            stem = os.path.splitext(name)[0]
            # Search output folder for any file starting with stem
            # e.g. stem_body.txt, stem_att_*
            for root, dirs, files in os.walk(folder):
                for f in files:
                    if f.lower().startswith(stem.lower() + "_body") or f.lower().startswith(stem.lower() + "_att"):
                        full_p = os.path.join(root, f)
                        if full_p not in files_to_attach:
                            files_to_attach.append(full_p)
                            
    return files_to_attach

def main():
    parser = argparse.ArgumentParser(
        description="Outlook extraction -> Azure storage -> PO pipeline (one process)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all emails (not just unread)",
    )
    parser.add_argument(
        "--folder",
        type=str,
        default="outlook_po_extracted",
        help="Folder for Outlook output and pipeline input",
    )
    parser.add_argument(
        "--skip-outlook",
        action="store_true",
        help="Skip Outlook extraction; run pipeline on existing folder only",
    )
    parser.add_argument(
        "--skip-extraction",
        action="store_true",
        help="Skip raw PDF/OCR extraction; run enrichment, export, and post-processing only",
    )
    parser.add_argument(
        "--no-azure",
        action="store_true",
        help="Skip Azure blob upload",
    )
    parser.add_argument(
        "--no-celonis",
        action="store_true",
        help="Skip pushing results to Celonis PO_EXTRACTION_RESULTS table",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=100,
        help="Max Outlook messages per run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode: extract + map + report only. Skips Celonis push (Step 3.5), CSR exception emails (Step 6), and Outlook folder moves (Step 7). Use this for Step 1 of the 3-step workflow so emails stay in the inbox for the real run.",
    )
    parser.add_argument(
        "--no-folder-move",
        action="store_true",
        help="Skip Step 7 (Outlook folder moves to Processed/Exception/Unprocessed). Useful when reviewing before committing.",
    )
    parser.add_argument(
        "--use-azure-mapper",
        action="store_true",
        default=True,
        help="Use Azure Blob + Decision Tree for order type / customer_group2 mapping (default: True)",
    )
    parser.add_argument(
        "--mail-folder",
        type=str,
        default=None,
        help="Read from a specific Outlook folder (e.g. 'Unprocessed POs')",
    )
    parser.add_argument(
        "--skip-celonis-refresh",
        action="store_true",
        default=False,
        help="Skip fetching fresh master data from Celonis (use cached data). Default: always refresh.",
    )
    args = parser.parse_args()

    folder = Path(args.folder)         # historical archive folder (outlook_po_extracted)
    folder.mkdir(parents=True, exist_ok=True)
    python_exe = sys.executable

    # ─────────────────────────────────────────────────────────────────────────
    # PER-RUN STAGING FOLDER  (outlook_po_current_run/)
    # ─────────────────────────────────────────────────────────────────────────
    # Only files from the CURRENT Outlook poll go here.
    # This folder is wiped clean at the start of every run so that
    # po_extraction_enhanced.py processes ONLY real POs from Inbox /
    # Unprocessed POs — never historical files, exception emails, or noise.
    # ─────────────────────────────────────────────────────────────────────────
    import shutil
    run_folder = Path("outlook_po_current_run")
    if run_folder.exists():
        shutil.rmtree(run_folder)
    run_folder.mkdir(parents=True, exist_ok=True)
    print(f"\n  [Pipeline] Per-run staging folder ready: {run_folder} (cleared)")

    # ── Step -1: Refresh Celonis Master Data (Test MP + Sheet 3 + Vendor KB) ──
    # Always fetch fresh data from Celonis at the start of every run so that
    # daily-refreshed data (Test MP customer master, Ship-to, materials) is used
    # for ALL mapping. This is the source of truth — stale cache = wrong mapping.
    if not args.skip_celonis_refresh:
        print("\n" + "=" * 60)
        print("  -1. Refreshing Celonis Master Data (Test MP + Sheet 3 + Vendor KB)")
        print("=" * 60)
        print("  -> Fetching fresh Test MP and Sheet 3 from Celonis -> Azure Blob...")
        ok_celonis = run_cmd(
            f'"{python_exe}" celonis_to_azure.py',
            "-1a. Celonis -> Azure Blob (Test MP + Sheet 3)",
        )
        if not ok_celonis:
            print("  [WARN] Celonis data fetch failed — continuing with cached data.")

        print("  -> Rebuilding vendor knowledge base from fresh Celonis analysis...")
        ok_kb = run_cmd(
            f'"{python_exe}" setup_knowledge_base.py',
            "-1b. Rebuild Vendor Knowledge Base (knowledge_base.db)",
        )
        if not ok_kb:
            print("  [WARN] Vendor KB rebuild failed — continuing with existing KB.")
    else:
        print("\n  [Celonis Refresh] Skipped (--skip-celonis-refresh). Using cached data.")

    # -- Step 0: Download pending files from Azure --
    if not args.no_azure:
        print("\n" + "=" * 60)
        print("  0. Syncing pending PDFs from Azure Blob Storage")
        print("=" * 60)
        sync_from_blob("input-po", folder)

    # -- Step 1: Outlook extraction --
    # Track ONLY the files extracted from Outlook in this run (not Azure Blob downloads)
    current_run_outlook_files = set()  # lowercase basenames of files from THIS mailbox poll

    if not args.skip_outlook:
        from outlook_poller import process_emails
        os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

        ok = False
        files = []
        try:
            # ── Write new PO files into the clean per-run staging folder ──
            # This ensures po_extraction_enhanced.py sees ONLY current POs.
            files = process_emails(
                all_emails=args.all,
                output_folder=str(run_folder),   # <-- per-run clean folder
                upload_to_azure=bool(os.getenv("AZURE_BLOB_URL")) and not args.no_azure,
                mark_read=not args.all,
                top=args.top,
                mail_folder=args.mail_folder,
            )
            ok = True
        except Exception as e:
            print(f"  [WARN] Outlook extraction ended early: {e}")
            # Recovery: scan run_folder for any PO files already saved before the crash.
            # This handles charmap / encoding errors on Chinese email subjects that crash
            # mid-run but after several files were already written to disk.
            _PO_EXTS = ('.pdf', '.txt', '.png', '.jpg', '.jpeg', '.xlsx', '.xls', '.docx')
            _SKIP_SUBS_RECOVER = ('po exception', 'ai processing failed', 'robona-attach', 'undeliverable')
            for _fp in run_folder.iterdir():
                if _fp.is_file() and _fp.suffix.lower() in _PO_EXTS:
                    _fn_lower = _fp.name.lower()
                    if not any(s in _fn_lower for s in _SKIP_SUBS_RECOVER):
                        if str(_fp) not in files:
                            files.append(str(_fp))
                            print(f"  [Recover] Recovered file from partial run: {_fp.name}")
            if files:
                print(f"  [WARN] Recovered {len(files)} file(s) from partial run — continuing pipeline.")
            else:
                print(f"  [WARN] No files found in '{run_folder}' — nothing to process.")

        if not files:
            print(f"\n  [Outlook] Found 0 new PO emails to process.")
            print(f"  [Outlook] Inbox and Unprocessed POs are up to date — nothing to extract.")
            print(f"  [Outlook] Tip: Run with --all to force-reprocess already-tracked emails.")
            # ── EXIT EARLY: no new emails means nothing to process this run ──
            # Do NOT fall through to scan the historical outlook_po_extracted/ folder.
            print("\n  [Pipeline] No new PO files — exiting cleanly.")
            sys.exit(0)
        else:
            # Populate current_run_outlook_files from recovered + returned files
            for fp in files:
                current_run_outlook_files.add(os.path.basename(fp).lower())
                # Also copy to the historical archive folder for reference
                try:
                    import shutil as _sh
                    _sh.copy2(fp, folder / os.path.basename(fp))
                except Exception:
                    pass
            print(f"\n  [Outlook] Successfully extracted {len(files)} PO file(s) from mailbox.")
            print(f"  [Outlook] Files written to per-run staging folder: {run_folder}")
            print(f"  [Outlook] Files: {[os.path.basename(f) for f in files]}")
    else:
        # --skip-outlook: use run_folder contents if already populated, else fall back to folder
        if not list(run_folder.iterdir()):
            print(f"Skipping Outlook (--skip-outlook). Using existing archive folder: {folder}")
            run_folder = folder   # fall back to historical folder when explicitly skipping outlook
        else:
            print(f"Skipping Outlook (--skip-outlook). Using per-run folder: {run_folder}")

    # -- Step 2: PO Extraction Pipeline --
    # Always use the CLEAN per-run folder (run_folder) for extraction.
    # This contains ONLY PO files from the current Inbox / Unprocessed POs poll.
    folder_basename = "outlook_po_extracted"   # keep output filenames consistent
    raw_out = f"results_{folder_basename}.jsonl"
    enriched_out = f"results_{folder_basename}_enriched.jsonl"
    csv_out = f"extracted_pos_{folder_basename}.csv"

    if not args.skip_extraction:
        cmd_extract = f'"{python_exe}" po_extraction_enhanced.py --folder "{run_folder}" --output "{raw_out}"'

        child_env = os.environ.copy()
        child_env["DISABLE_MODEL_SOURCE_CHECK"] = "True"

        print(f"\n{'='*60}")
        print(f"  STEP: 1. PDF/OCR Extraction")
        print(f"  Source folder: {run_folder} ({len(list(run_folder.iterdir()))} file(s))")
        print(f"  Command: {cmd_extract}")
        print(f"{'='*60}")

        res = subprocess.run(cmd_extract, shell=True, env=child_env)
        if res.returncode != 0:
            print(f"\n[ERROR]: Step '1. PDF/OCR Extraction' failed with exit code {res.returncode}")
            sys.exit(1)
        print(f"\n[SUCCESS]: 1. PDF/OCR Extraction completed.")
    else:
        print("\nSkipping raw extraction (--skip-extraction). Using existing raw output.")

    azure_mapper_flag = "--use-azure-mapper" if getattr(args, 'use_azure_mapper', True) else ""
    cmd_enrich = f'"{python_exe}" reenrich_results.py --input "{raw_out}" --output "{enriched_out}" {azure_mapper_flag}'
    if not run_cmd(cmd_enrich, "2. KB Enrichment & Excel Mapping (Azure Mapper)"):
        sys.exit(1)

    # ── Step 2.5: Automatic Pre-flight Validation Gate ──────────────────────
    # Runs BEFORE CSV export and Celonis push.
    # Any PO that fails validation (unmapped materials, blank sold-to/ship-to,
    # zero quantity, missing sales org) is:
    #   1. Removed from the enriched JSONL so it is NEVER pushed to Celonis
    #   2. Added to preflight_failed_files so it gets routed to exception email
    #      in Step 6, same as a MAPPING_FAILED PO.
    # This prevents wrong SAP Sales Orders from being created in production.
    # ─────────────────────────────────────────────────────────────────────────
    preflight_failed_files = {}   # source_file_lower → {po_number, issues, missing_materials}
    _VALID_UNITS = {
        "KG", "KGM", "G", "GRM", "TO", "TNE", "MT", "LB", "LBR", "OZ", "T", "ST", "LT",
        "吨", "噸", "公吨", "公噸",
    }

    try:
        import json as _pf_json
        from pathlib import Path as _PF_Path

        _enr_path = _PF_Path(enriched_out)
        if _enr_path.exists():
            _good_lines = []
            _fail_lines = []

            with open(_enr_path, "r", encoding="utf-8", errors="replace") as _pf_fh:
                for _pf_line in _pf_fh:
                    _pf_line = _pf_line.strip()
                    if not _pf_line:
                        continue
                    try:
                        _pf_obj = _pf_json.loads(_pf_line)
                    except Exception:
                        _good_lines.append(_pf_line)
                        continue

                    _hdr  = _pf_obj.get("header_fields") or {}
                    _sos  = _pf_obj.get("sales_orders")  or []
                    _src  = str(_pf_obj.get("source_file") or "").strip()
                    _pnum = str(_hdr.get("po_number") or "").strip() or _src
                    _pf_issues   = []
                    _pf_unmapped = []

                    # ── Check 0: customer match escalated by fuzzy guard ──────────────
                    # This is the highest-priority check: if reenrich_results.py
                    # flagged this PO as ambiguous/low-confidence, add a clear
                    # human-readable issue that names the candidate SAP IDs.
                    _esc_reason     = _hdr.get("escalation_reason", "")
                    _esc_candidates = _hdr.get("escalation_candidates", []) or []
                    _esc_score      = _hdr.get("escalation_top_score", 0.0)
                    _cust_escalated = bool(_hdr.get("customer_match_escalated"))
                    if _cust_escalated:
                        _cands_str = ", ".join(str(c) for c in _esc_candidates) if _esc_candidates else "unknown"
                        if _esc_reason == "customer_ambiguous":
                            _pf_issues.append(
                                f"Customer match AMBIGUOUS — {len(_esc_candidates)} SAP IDs tied: "
                                f"{_cands_str} (top score={_esc_score:.2f}). Human review required."
                            )
                        else:
                            _pf_issues.append(
                                f"Customer match LOW CONFIDENCE — best candidate: {_cands_str[0:60]} "
                                f"(score={_esc_score:.2f} < threshold). Human review required."
                            )

                    # Check 1: at least one SO generated
                    if not _sos and not _cust_escalated:
                        _pf_issues.append("No Sales Order generated — customer/material not mapped")

                    # Check 2: sold-to present (skip when escalated — blank is expected)
                    if not _cust_escalated:
                        _pf_cust = str(
                            _hdr.get("customer_number") or _hdr.get("sold_to_id") or ""
                        ).strip()
                        if not _pf_cust:
                            _pf_issues.append("Sold-to party is blank")

                    # Check 3: sales org + ship-to per SO (skip if escalated)
                    if not _cust_escalated:
                        _pf_sorg_found = False
                        for _pf_so in _sos:
                            _pf_sorg = str(_pf_so.get("sales_organization") or "").strip()
                            if _pf_sorg:
                                _pf_sorg_found = True
                            _pf_st = str(_pf_so.get("ship_to_id") or "").strip()
                            if not _pf_st:
                                _pf_issues.append(
                                    f"Ship-to ID blank for SO grouping "
                                    f"'{_pf_so.get('so_grouping_label', '?')}'"
                                )
                        if not _pf_sorg_found and _sos:
                            _pf_issues.append("Sales Organization is blank")

                    # Check 4: materials + quantities per line item (skip if escalated)
                    if not _cust_escalated:
                        for _pf_so in _sos:
                            for _pf_it in (_pf_so.get("items") or []):
                                _pf_mat_int = str(
                                    _pf_it.get("internal_material_number") or ""
                                ).strip()
                                _pf_mat_ext = (
                                    _pf_it.get("extracted_material_number")
                                    or _pf_it.get("customer_material_number")
                                    or _pf_it.get("material_description")
                                    or "?"
                                )
                                _pf_ln = _pf_it.get("line_number") or "?"
                                _pf_qty_raw = _pf_it.get("quantity")
                                _pf_unit    = str(_pf_it.get("unit") or "").strip().upper()

                                # Unmapped material?
                                if not _pf_mat_int or _pf_mat_int in ("", "None", "nan", "0"):
                                    _pf_unmapped.append(f"Line {_pf_ln}: '{_pf_mat_ext}'")

                                # Invalid quantity?
                                try:
                                    _pf_qty = float(
                                        str(_pf_qty_raw).replace(",", "").strip()
                                    )
                                    if _pf_qty <= 0:
                                        _pf_issues.append(
                                            f"Line {_pf_ln}: quantity is zero or negative ({_pf_qty})"
                                        )
                                except (ValueError, TypeError):
                                    _pf_issues.append(
                                        f"Line {_pf_ln}: quantity '{_pf_qty_raw}' is not a valid number"
                                    )

                    if _pf_unmapped:
                        _pf_issues.append(
                            f"Unmapped material(s): "
                            + " | ".join(_pf_unmapped)
                        )

                    if _pf_issues:
                        # This PO FAILS pre-flight → strip from enriched output
                        _fail_lines.append(_pf_line)
                        _src_lower = _src.lower()
                        preflight_failed_files[_src_lower] = {
                            "po_number":            _pnum,
                            "issues":               _pf_issues,
                            "missing_materials":    _pf_unmapped,
                            "escalation_reason":    _esc_reason,
                            "escalation_candidates": _esc_candidates,
                            "escalation_score":     _esc_score,
                            "customer_escalated":   _cust_escalated,
                            "header_fields":        _hdr,
                        }
                        print(
                            f"  [PRE-FLIGHT FAIL] PO '{_pnum}' ({_src}) — "
                            f"BLOCKED from SO creation: "
                            + "; ".join(_pf_issues)
                        )
                    else:
                        _good_lines.append(_pf_line)

            # Re-write enriched JSONL with only GOOD records and log failures to audit
            if preflight_failed_files:
                _audit = get_audit_logger()
                with open(_enr_path, "w", encoding="utf-8") as _pf_out:
                    for _gl in _good_lines:
                        _pf_out.write(_gl + "\n")
                # Log each failed PO to the audit trail
                for _sf, _fdata in preflight_failed_files.items():
                    _audit.log(
                        po_number=_fdata.get("po_number", _sf),
                        source_file=_sf,
                        extraction_complete=True,
                        action="ESCALATED",
                        escalation_reason="preflight_validation_failed: " + "; ".join(_fdata.get("issues", [])),
                        validation_checks={
                            iss: "FAIL" for iss in _fdata.get("issues", [])
                        },
                    )
                print(
                    f"\n  [PRE-FLIGHT] Gate complete — "
                    f"{len(_good_lines)} PO(s) PASS (will create SO), "
                    f"{len(preflight_failed_files)} PO(s) FAIL (routed to exception)."
                )
            else:
                print(
                    f"  [PRE-FLIGHT] All {len(_good_lines)} PO(s) passed validation — proceeding to SO creation."
                )
    except Exception as _pf_err:
        print(f"  [PRE-FLIGHT-WARN] Validation gate error (pipeline will continue): {_pf_err}")
        import traceback as _pf_tb
        _pf_tb.print_exc()

    cmd_export = f'"{python_exe}" run_test_export.py --input "{enriched_out}" --output "{csv_out}"'
    export_ok = run_cmd(cmd_export, "3. CSV Export")
    if not export_ok:
        print("  [WARN] CSV Export failed — pipeline will continue. Check run_test_export.py for details.")
        print(f"  [WARN] Input file: {enriched_out}  Output: {csv_out}")

    # -- Step 3.5: Push to Celonis --
    if args.dry_run:
        print("\n  [DRY-RUN] Skipping Celonis push (Step 3.5) — use 'python run_outlook_to_pipeline.py' for the real run.")
    elif not args.no_celonis:
        # ── Detect DUPLICATE ALLOWED POs before pushing ──────────────────────
        # If a PO was previously pushed to Celonis but no SAP Sales Order was created
        # (the Azure Tracker shows it as PUSHED_TO_CELONIS for >0h with no Celonis result),
        # the push registry still has its old dedup hash → push_to_celonis.py would SKIP it.
        # Fix: collect those PO numbers and pass them as --force-po-numbers so the registry
        # key is evicted and the data is re-pushed to trigger the Celonis Action Flow again.
        force_po_numbers_list = []
        try:
            import json as _json
            from pathlib import Path as _Path
            _enriched_path = _Path(enriched_out)
            if _enriched_path.exists():
                # Build PO → source_file map from current enriched output
                _po_to_src = {}
                with open(_enriched_path, "r", encoding="utf-8", errors="replace") as _ef:
                    for _line in _ef:
                        if not _line.strip():
                            continue
                        try:
                            _obj = _json.loads(_line)
                            _po  = str((_obj.get("header_fields") or {}).get("po_number") or "").strip()
                            _src = str(_obj.get("source_file") or "").strip().lower()
                            if _po and _src:
                                _po_to_src[_src] = _po
                        except Exception:
                            pass

                # Query Azure Tracker for emails eligible for Stage 1 re-processing
                # (those previously pushed but with no SO result yet — DUPLICATE ALLOWED)
                try:
                    from azure_email_tracker import AzureEmailTracker as _AET
                    _trk = _AET()
                    _prev_pushed = _trk.get_eligible_emails(
                        ["PUSHED_TO_CELONIS", "PENDING_STAGE2", "MAPPED_SUCCESS"]
                    )
                    _prev_src_set = {
                        str(r.get("source_file") or "").strip().lower()
                        for r in _prev_pushed
                    }
                    # A PO is DUPLICATE ALLOWED if its source file appears in both the
                    # current enriched JSONL and the previous-pushed set in Azure Tracker
                    for _src_lc, _po_num in _po_to_src.items():
                        if _src_lc in _prev_src_set and _po_num:
                            force_po_numbers_list.append(_po_num)
                            print(f"  [Step3.5] DUPLICATE ALLOWED: PO '{_po_num}' ({_src_lc}) "
                                  f"— will evict registry key and force re-push to Celonis.")
                except Exception as _trk_err:
                    print(f"  [Step3.5-WARN] Could not check Azure Tracker for DUPLICATE POs: {_trk_err}")
        except Exception as _pre_err:
            print(f"  [Step3.5-WARN] Pre-read of enriched JSONL failed: {_pre_err}")

        # Build the push command — add --force-po-numbers only if there are duplicates to force
        force_flag = ""
        if force_po_numbers_list:
            force_str  = ",".join(force_po_numbers_list)
            force_flag = f' --force-po-numbers "{force_str}"'

        cmd_celonis = f'"{python_exe}" push_to_celonis.py --input "{csv_out}"{force_flag}'
        if not run_cmd(cmd_celonis, "3.5 Push to Celonis (PO_EXTRACTION_RESULTS)"):
            print("  [WARN] Celonis push failed, continuing with pipeline...")



    # -- Step 4: Upload Results to Azure --
    if not args.no_azure:
        print("\n" + "=" * 60)
        print("  4. Uploading Results to Azure Blob Storage")
        print("=" * 60)
        upload_to_blob("output-results", Path(raw_out))
        upload_to_blob("output-results", Path(enriched_out))
        upload_to_blob("output-results", Path(csv_out))

    # -- Step 5: Mark emails as Processed in DB --
    try:
        import sqlite3
        import json
        db_path = Path(__file__).parent / "processed_emails.db"

        # ── FIX 1: open with utf-8 to avoid charmap errors ──
        # ── FIX 2: determine SUCCESS by checking actual extraction output,
        #           NOT by matching msg_id[:8] in filenames (unreliable when
        #           attachments keep their original names).
        # Strategy:
        #   - Build a set of filenames for each status based on enriched output:
        #     * success_files: at least 1 sales order, and NO missing material codes
        #     * missing_material_files: at least 1 sales order, but has missing material codes
        #     * failed_files: no sales orders or items
        #   - Match DB subjects/filenames against these sets

        # Use ONLY files from the current Outlook mailbox poll (tracked in current_run_outlook_files).
        # This excludes historical files downloaded from Azure Blob Storage (Step 0) so
        # the accuracy metric reflects only what was extracted from the inbox today.
        if current_run_outlook_files:
            # New emails were polled and staged to 'Unprocessed POs' this run
            current_run_filter = current_run_outlook_files
            n_files = len(current_run_outlook_files)
            run_source_label = (
                f"Unprocessed POs folder — {n_files} PO file(s) extracted from "
                f"{len(current_run_outlook_files)} new inbox email(s) this run"
            )
        elif args.skip_outlook:
            # --skip-outlook mode: no Outlook poll, use folder contents for testing
            current_run_filter = set()
            if folder.exists():
                for item in folder.iterdir():
                    if item.is_file():
                        current_run_filter.add(item.name.lower())
            run_source_label = f"folder '{folder.name}' ({len(current_run_filter)} file(s)) [--skip-outlook mode]"
        else:
            # Outlook was polled but returned 0 new emails — inbox is empty/all processed.
            # Use None (not empty set) so the filter check below correctly skips all files
            # and shows 0 instead of falling through to count all 126 historical files.
            current_run_filter = None  # None = "no new files this run" (shows 0)
            run_source_label = "Unprocessed POs folder — 0 new emails (inbox is up to date)"

        success_files = set()          # source_file values with completely mapped output (all files, for DB matching)
        missing_material_files = set() # source_file values with missing material mapping (all files)
        failed_files  = set()          # source_file values with no output (all files)
        file_info_map = {}             # map of file name -> details (all files)

        # Separate collections strictly for printed metrics (filtered to current run files)
        metric_success = set()
        metric_missing = set()
        metric_failed = set()

        # ── Inject pre-flight failures into classification ─────────────────────
        # POs blocked by Step 2.5 pre-flight gate are treated identically to
        # MAPPING_FAILED POs: they go into missing_material_files and file_info_map
        # so the exception email is sent automatically in Step 6.
        for _pf_src, _pf_data in preflight_failed_files.items():
            missing_material_files.add(_pf_src)
            _pf_hdr = _pf_data.get("header_fields") or {}
            file_info_map[_pf_src] = {
                "po_number":            _pf_data.get("po_number", ""),
                "sales_org":            _pf_hdr.get("sales_organization") or None,
                "missing_materials":    _pf_data.get("missing_materials") or _pf_data.get("issues", []),
                "so_numbers":           [],
                "header_fields":        _pf_hdr,
                # Escalation data — forwarded to exception email builder
                "escalation_reason":    _pf_data.get("escalation_reason", ""),
                "escalation_candidates": _pf_data.get("escalation_candidates", []),
                "escalation_score":     _pf_data.get("escalation_score", 0.0),
                "customer_escalated":   _pf_data.get("customer_escalated", False),
            }
            # Also add to metric_missing if within current run scope
            if current_run_filter is None or (not current_run_filter or _pf_src in current_run_filter):
                metric_missing.add(_pf_src)


        if Path(enriched_out).exists():
            with open(enriched_out, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                        src = obj.get("source_file", "")
                        if not src:
                            continue

                        sos = obj.get("sales_orders", [])
                        has_items = any(
                            len(so.get("items", [])) > 0 for so in sos
                        ) if sos else False

                        # Find sales org
                        sales_org = None
                        if sos:
                            sales_org = sos[0].get("sales_organization")
                        if not sales_org:
                            header = obj.get("header_fields", {})
                            sales_org = header.get("sales_organization")

                        # Find missing materials
                        missing_mats = []
                        if has_items:
                            for so in sos:
                                for item in so.get("items", []):
                                    mat_internal = item.get("internal_material_number")
                                    if not mat_internal or str(mat_internal).strip() in ("", "None", "nan"):
                                        missing_mats.append(
                                            item.get("extracted_material_number")
                                            or item.get("material_description")
                                            or "Unknown Material"
                                        )

                        # Collect SAP SO numbers from the enriched record
                        so_number_list = []
                        po_number_raw = ""
                        header_fields = obj.get("header_fields", {})
                        po_number_raw = header_fields.get("po_number", "") or ""
                        for so in sos:
                            so_num = so.get("so_number") or so.get("sales_order_number") or ""
                            if so_num and str(so_num).strip() not in ("", "None", "nan"):
                                        so_number_list.append(str(so_num).strip())

                        # Populate master lookup for DB mapping
                        file_info_map[src.lower()] = {
                            "sales_org": sales_org,
                            "missing_materials": missing_mats,
                            "so_numbers": so_number_list,
                            "po_number": po_number_raw,
                            "header_fields": obj.get("header_fields", {}),
                        }

                        # ------------------------------------------------------------------
                        # GUARD: Never add inline email images (image001.png, image.png, etc.)
                        # or email body txt files (body_*.txt) to the failure/success lists.
                        # These are not PO documents — adding them causes false exceptions.
                        # ------------------------------------------------------------------
                        import re as _re2
                        _is_inline_img = bool(_re2.match(
                            r'^image\d*(_[a-z0-9]+)?\.(png|jpg|jpeg)$', src.lower()
                        ))
                        _is_body_txt = src.lower().startswith('body_') and src.lower().endswith('.txt')
                        if _is_inline_img or _is_body_txt:
                            continue  # skip entirely — not a PO document

                        if has_items:
                            if missing_mats:
                                missing_material_files.add(src.lower())
                            else:
                                success_files.add(src.lower())
                        else:
                            failed_files.add(src.lower())

                        # Populate metrics lists ONLY if part of the current run's files
                        if current_run_filter is not None and (not current_run_filter or src.lower() in current_run_filter):
                            if has_items:
                                if missing_mats:
                                    metric_missing.add(src.lower())
                                else:
                                    metric_success.add(src.lower())
                            else:
                                metric_failed.add(src.lower())

                    except Exception:
                        pass

        total_files = len(metric_success) + len(metric_missing) + len(metric_failed)
        accuracy = (len(metric_success) / total_files * 100) if total_files > 0 else 0.0

        print("\n" + "=" * 60)
        print("  5. Current Run Summary & Email Tracking Update")
        print("=" * 60)
        print(f"  Source              : {run_source_label}")
        if current_run_filter is None:
            # No new emails polled this run — all counters are 0
            print(f"  Total PO Files      : 0 (no new emails in inbox)")
            print(f"  Mapped Success      : 0")
            print(f"  Missing Materials   : 0")
            print(f"  Extraction Failed   : 0")
            print(f"  Current Run Accuracy: N/A (inbox up to date — no new emails processed)")
        else:
            print(f"  Total PO Files      : {total_files} file(s) from current run")
            print(f"  Mapped Success      : {len(metric_success)} file(s)")
            print(f"  Missing Materials   : {len(metric_missing)} file(s)")
            print(f"  Extraction Failed   : {len(metric_failed)} file(s)")
            if total_files > 0:
                print(f"  Current Run Accuracy: {accuracy:.1f}%")
            else:
                print(f"  Current Run Accuracy: N/A")

        # ── Inline Diagnostic Report (scoped to this run only) ─────────────────
        # Rebuild richer detail from the enriched file for the diagnostic printout.
        # We already have file_info_map built below; we need to also capture
        # per-file customer + extraction-error details. We do a second pass here
        # using the data we already collected in success_files / missing_material_files
        # / failed_files + file_info_map.
        #
        # NOTE: file_info_map is built in the loop BELOW this block, so we
        # collect the diagnostic data inline during the same loop pass and print
        # it afterwards. To keep the code readable we split the loop:
        #  Pass A (below): build file_info_map + classify files  <-- existing code
        #  Pass B (here):  use those results for the diagnostic printout

        # --- diagnostic detail lists (filled after the enriched-file loop) ---
        _diag_failed   = []   # list of dicts for extraction failures
        _diag_missing  = []   # list of dicts for missing-material POs
        _diag_built    = False  # flag so we only build once

        def _build_diagnostic(enriched_path, run_filter):
            """Return (failed_list, missing_list) for the current run."""
            failed_list  = []
            missing_list = []
            if not enriched_path.exists():
                return failed_list, missing_list
            with open(enriched_path, "r", encoding="utf-8", errors="replace") as _fh:
                for _ln in _fh:
                    _ln = _ln.strip()
                    if not _ln:
                        continue
                    try:
                        _obj = json.loads(_ln)
                    except Exception:
                        continue
                    _src = _obj.get("source_file", "")
                    if not _src:
                        continue
                    # None = no new emails this run — skip all diagnostic output
                    if run_filter is None:
                        continue
                    if run_filter and _src.lower() not in run_filter:
                        continue
                    _hdr   = _obj.get("header_fields", {})
                    _items = _obj.get("line_items", [])
                    _sos   = _obj.get("sales_orders", [])
                    _err   = _obj.get("error")
                    _po    = _hdr.get("po_number") or "?"
                    _cust  = (
                        _hdr.get("customer_name_matched")
                        or _hdr.get("customer_id_or_name")
                        or _hdr.get("customer_name")
                        or "?"
                    )
                    _cust_id = _hdr.get("customer_number") or _hdr.get("customer_id") or "?"

                    if _err or not _items:
                        failed_list.append({
                            "file": _src, "po": _po, "customer": _cust,
                            "reason": (_err or "No line items parsed from PO")[:100],
                        })
                        continue

                    _missing_in_po = []
                    for _so in _sos:
                        for _it in _so.get("items", []):
                            _mat_int = _it.get("internal_material_number", "")
                            if not _mat_int or str(_mat_int).strip() in ("", "None", "nan", "0"):
                                _missing_in_po.append({
                                    "mat_code": (_it.get("extracted_material_number")
                                                 or _it.get("customer_material_number") or "?"),
                                    "mat_desc": (_it.get("material_description") or "?")[:55],
                                })
                    if _missing_in_po or not _sos:
                        missing_list.append({
                            "file": _src, "po": _po,
                            "customer": _cust, "customer_id": _cust_id,
                            "missing_items": _missing_in_po or [
                                {"mat_code": "N/A",
                                 "mat_desc": "No Sales Order (customer/material combo missing)"}
                            ],
                        })
                    # fully mapped — no action needed in diagnostics
            return failed_list, missing_list

        _diag_failed, _diag_missing = _build_diagnostic(
            Path(enriched_out), current_run_filter
        )

        # ── Print diagnostic: Extraction Failures ──────────────────────────────
        if _diag_failed:
            print("\n" + "-" * 60)
            print(f"  DIAGNOSTIC — Extraction Failed ({len(_diag_failed)} PO file(s))")
            print(f"  (Cannot parse line items — usually email body .txt without PDF)")
            print("-" * 60)
            for _i, _r in enumerate(_diag_failed, 1):
                _fname = _r["file"]
                if len(_fname) > 55:
                    _fname = "..." + _fname[-52:]
                print(f"  [{_i:2}] File    : {_fname}")
                print(f"       PO      : {_r['po']}")
                print(f"       Customer: {_r['customer']}")
                print(f"       Reason  : {_r['reason']}")
        else:
            print("\n  DIAGNOSTIC: No extraction failures in this run.")

        # ── Print diagnostic: Missing Materials ────────────────────────────────
        if _diag_missing:
            from collections import defaultdict as _dd
            _by_cust = _dd(list)
            for _r in _diag_missing:
                _by_cust[(_r["customer"], _r["customer_id"])].append(_r)

            print("\n" + "-" * 60)
            print(f"  DIAGNOSTIC — Missing Materials ({len(_diag_missing)} PO file(s))")
            print(f"  Add these customer/material combos to Celonis master data:")
            print("-" * 60)
            for (_cust, _cid), _recs in sorted(_by_cust.items()):
                print(f"\n  Customer : {_cust}  (ID: {_cid})")
                # De-duplicate material codes for this customer
                _seen_mats = set()
                for _rec in _recs:
                    print(f"    PO: {_rec['po']}  |  File: {_rec['file']}")
                    for _m in _rec["missing_items"]:
                        _key = (_m["mat_code"], _m["mat_desc"])
                        if _key not in _seen_mats:
                            print(f"      -> Material Code: {_m['mat_code']}")
                            print(f"         Description : {_m['mat_desc']}")
                            _seen_mats.add(_key)
        else:
            print("  DIAGNOSTIC: All materials mapped successfully in this run.")

        print()


        # ── IMPORTANT: use AzureEmailTracker (not raw SQLite) so that PENDING records
        # written to Azure Table Storage by the Outlook poller are visible here.
        # Previously this block used sqlite3 directly, which missed all records when
        # Azure Table Storage is the active backend (SQLite had 0 PENDING rows).
        multi_fail_map = {}
        if success_files or missing_material_files or failed_files:
            from azure_email_tracker import AzureEmailTracker as _Tracker
            _tracker5 = _Tracker()

            # Helper for matching processed files to tracker attachment records
            def is_file_match(p_file: str, db_file: str) -> bool:
                pf = p_file.lower().strip()
                df = db_file.lower().strip()
                if pf == df:
                    return True
                db_stem = os.path.splitext(df)[0]
                p_stem  = os.path.splitext(pf)[0]
                if len(db_stem) > 4 and (db_stem in pf or pf in db_stem):
                    return True
                if len(p_stem) > 4 and (p_stem in df or df in p_stem):
                    return True
                return False

            # Build attachment map from tracker (covers BOTH Azure Table and SQLite backend)
            # tracker.get_eligible_emails fetches all PENDING rows regardless of backend.
            msg_att_map = {}

            # Also pull from email_attachments SQLite table as secondary source
            if db_path.exists():
                try:
                    _conn_att = sqlite3.connect(db_path)
                    _cur_att  = _conn_att.cursor()
                    _cur_att.execute("SELECT message_id, filename FROM email_attachments")
                    for _mid, _fname in _cur_att.fetchall():
                        if _fname:
                            msg_att_map.setdefault(_mid, []).append(_fname.strip())
                    _cur_att.execute(
                        "SELECT message_id, attachments FROM processed_emails WHERE attachments IS NOT NULL"
                    )
                    for _mid, _att_str in _cur_att.fetchall():
                        if _att_str:
                            for _f in _att_str.split(","):
                                _f = _f.strip()
                                if _f and _f not in msg_att_map.get(_mid, []):
                                    msg_att_map.setdefault(_mid, []).append(_f)
                    _conn_att.close()
                except Exception as _ae:
                    print(f"  [WARN] Could not read email_attachments from SQLite: {_ae}")

            # Auto-mark system-generated emails stuck as PENDING -> SKIP via tracker
            SYSTEM_SUBJECT_PREFIXES = (
                "[robona-attach]",
                "[po exception",
                "re: [robona",
                "fwd: [robona",
                "re: [po exception",
            )

            # Fetch ALL PENDING emails from tracker (Azure Table or SQLite)
            all_pending_rows = _tracker5.get_eligible_emails(["PENDING"])
            skip_count = 0
            for _row in all_pending_rows:
                _mid  = _row.get("message_id", "") or _row.get("RowKey", "")
                _subj = (_row.get("subject", "") or "").lower().strip()
                if any(_subj.startswith(p) for p in SYSTEM_SUBJECT_PREFIXES):
                    _tracker5.update_status(_mid, "SKIP")
                    skip_count += 1
                    # Also pull attachment info into map from tracker's 'attachments' field
                    _atts_str = (_row.get("attachments", "") or "")
                    for _f in _atts_str.split(","):
                        _f = _f.strip()
                        if _f:
                            msg_att_map.setdefault(_mid, []).append(_f)
            if skip_count:
                print(f"  -> Auto-marked {skip_count} system-generated email(s) as SKIP")

            # Re-fetch PENDING rows after auto-skip (excludes newly SKIP'd ones)
            pending_email_rows = _tracker5.get_eligible_emails(["PENDING"])

            # Merge attachment info from tracker's 'attachments' property into msg_att_map
            for _row in pending_email_rows:
                _mid      = _row.get("message_id", "") or _row.get("RowKey", "")
                _atts_str = (_row.get("attachments", "") or "")
                for _f in _atts_str.split(","):
                    _f = _f.strip()
                    if _f and _f not in msg_att_map.get(_mid, []):
                        msg_att_map.setdefault(_mid, []).append(_f)

            success_count = 0
            missing_count = 0
            failed_count  = 0
            multi_fail_map = {}

            for _row in pending_email_rows:
                msg_id  = _row.get("message_id", "") or _row.get("RowKey", "")
                subject = _row.get("subject", "") or ""
                subject_lower = subject.lower()
                atts = msg_att_map.get(msg_id, [])

                if not atts:
                    # Email has no PO attachments -> mark SKIP
                    _tracker5.update_status(msg_id, "SKIP",
                                            extra_fields={"source_file": "no_po_attachment"})
                    print(f"    [SKIP_NO_ATTACHMENT] msg_id={msg_id[:20]} subject={subject[:50] if subject else '?'}")
                    continue

                def _is_junk_file(fname: str) -> bool:
                    fn = fname.lower()
                    if bool(re.match(r'^image\d*(_[a-z0-9]+)?\.(png|jpg|jpeg)$', fn)):
                        return True
                    if fn.startswith("body_") and fn.endswith(".txt"):
                        return True
                    return False

                all_success_files  = []
                all_missing_files  = []
                all_failed_files   = []

                # 1. Primary match: attachment filename -> pipeline output file
                for att in atts:
                    if _is_junk_file(att):
                        continue
                    for sf in success_files:
                        if not _is_junk_file(sf) and is_file_match(sf, att) and sf not in all_success_files:
                            all_success_files.append(sf)
                    for mmf in missing_material_files:
                        if not _is_junk_file(mmf) and is_file_match(mmf, att) and mmf not in all_missing_files:
                            all_missing_files.append(mmf)
                    for ff in failed_files:
                        if not _is_junk_file(ff) and is_file_match(ff, att) and ff not in all_failed_files:
                            all_failed_files.append(ff)

                # 2. Fallback: subject-line matching
                if not (all_success_files or all_missing_files or all_failed_files):
                    for sf in success_files:
                        if not _is_junk_file(sf) and (sf in subject_lower or subject_lower in sf):
                            all_success_files.append(sf)
                    for mmf in missing_material_files:
                        if not _is_junk_file(mmf) and (mmf in subject_lower or subject_lower in mmf):
                            all_missing_files.append(mmf)
                    for ff in failed_files:
                        if not _is_junk_file(ff) and (ff in subject_lower or subject_lower in ff):
                            all_failed_files.append(ff)

                any_failure = all_missing_files or all_failed_files

                if any_failure:
                    primary_fail = (all_missing_files + all_failed_files)[0]
                    db_status    = "EXTRACTION_FAILED" if (not all_missing_files and all_failed_files) else "MAPPING_FAILED"
                    _tracker5.update_status(msg_id, db_status,
                                            extra_fields={"source_file": primary_fail})
                    missing_count += 1
                    multi_fail_map[msg_id] = {
                        "missing":  all_missing_files,
                        "failed":   all_failed_files,
                        "success":  all_success_files,
                    }
                    if all_success_files:
                        print(f"    [{db_status}]  {primary_fail}  (PARTIAL: {len(all_success_files)} OK, {len(all_missing_files + all_failed_files)} failed)")
                    else:
                        info = file_info_map.get(primary_fail, {})
                        missing_desc = ', '.join(info.get('missing_materials', [])) or 'unknown'
                        print(f"    [{db_status}]  {primary_fail}  (missing: {missing_desc[:60]})")

                elif all_success_files:
                    _tracker5.update_status(msg_id, "MAPPED_SUCCESS",
                                            extra_fields={"source_file": all_success_files[0]})
                    success_count += 1
                    print(f"    [MAPPED_SUCCESS]    {all_success_files[0]}")

                else:
                    _tracker5.update_status(msg_id, "EXTRACTION_FAILED")
                    failed_count += 1
                    print(f"    [EXTRACTION_FAILED] msg_id={msg_id[:20]}  subject={subject[:50] if subject else '?'} (no PO items parsed)")

            print(f"  -> Marked {success_count} email(s) as MAPPED_SUCCESS")
            print(f"  -> Marked {missing_count} email(s) as MAPPING_FAILED")
            print(f"  -> Marked {failed_count} email(s) as EXTRACTION_FAILED")
        else:
            print("  -> No DB or no output to match — skipping status update.")

    except Exception as e:
        print(f"  -> Error updating tracking database: {e}")

    # -- Step 6: Stage 1 Post-Processing --
    # ─────────────────────────────────────────────────────────────────────────────
    # STAGE 1 EXCEPTIONS — raised immediately when material mapping / extraction fails.
    # These are terminal failures: pipeline cannot proceed to SAP, so CSR must handle manually.
    #
    # For MAPPED_SUCCESS emails:
    #   - Update tracker -> PUSHED_TO_CELONIS (data already sent in Step 3.5)
    #   - Do NOT archive or send Robona here — SAP SO does not exist yet.
    #   - Robona is triggered in Stage 2 (run_celonis_feedback.py) after Celonis confirms SO.
    #
    # For MAPPING_FAILED / EXTRACTION_FAILED / PENDING:
    #   - Forward original PO email to regional CSR
    #   - Send structured Stage 1 exception email with PDF attached
    #   - Update tracker -> EXCEPTION_ROUTED
    #   - Email will be moved to 'Exception POs' folder in Step 7
    # ─────────────────────────────────────────────────────────────────────────────
    try:
        from graph_email_handler import GraphEmailHandler

        print("\n" + "=" * 60)
        print("  6. Stage 1 Post-Processing: Exception Routing (Mapping/Extraction failures)")
        print("=" * 60)

        is_dry_run = getattr(args, 'dry_run', False)
        handler = GraphEmailHandler()

        # Use AzureEmailTracker to fetch emails that need post-processing.
        # This is the same tracker used in Step 5 and the Outlook poller — it reads
        # from Azure Table Storage (or SQLite fallback) so status updates from Step 5
        # are visible here regardless of backend.
        from azure_email_tracker import AzureEmailTracker as _Tracker6
        _tracker6 = _Tracker6()

        # Fetch emails needing post-processing (MAPPED_SUCCESS + failure states).
        _post_statuses = ["MAPPED_SUCCESS", "MAPPING_FAILED", "EXTRACTION_FAILED", "PENDING"]
        recent_emails_raw = _tracker6.get_eligible_emails(_post_statuses)
        # Convert to (msg_id, status, source_file, subject) tuples
        recent_emails = [
            (
                r.get("message_id") or r.get("RowKey", ""),
                r.get("status", ""),
                r.get("source_file", ""),
                r.get("subject", ""),
            )
            for r in recent_emails_raw
        ]

        # Build attachment lookup for exception emails (SQLite email_attachments table)
        msg_attachment_map = {}
        if db_path.exists():
            try:
                import sqlite3 as _sq6
                _conn6 = _sq6.connect(db_path)
                _cur6  = _conn6.cursor()
                _cur6.execute("SELECT ea.message_id, ea.filename FROM email_attachments ea")
                for _mid6, _fname6 in _cur6.fetchall():
                    if _mid6 not in msg_attachment_map:
                        msg_attachment_map[_mid6] = _fname6
                _conn6.close()
            except Exception as _e:
                print(f"  [WARN] Could not build attachment lookup: {_e}")

        pushed_count    = 0
        exception_count = 0
        sent_exception_ids = set()  # dedup guard — prevent double-sending within same run

        for msg_id, status, source_file, email_subject in recent_emails:
            sf_info      = file_info_map.get((source_file or "").lower(), {})
            sales_org    = sf_info.get("sales_org")
            missing_mats = sf_info.get("missing_materials", [])
            po_number    = sf_info.get("po_number", "")

            if status == "MAPPED_SUCCESS":
                # ── DUPLICATE PO NUMBER GUARD ─────────────────────────────────
                # Before pushing to Celonis, check whether this PO number was
                # already successfully pushed and resulted in a Sales Order (or is in-flight).
                if po_number:
                    def has_active_or_successful_so(check_po: str, check_sf: str, current_msg_id: str) -> bool:
                        # ── Check 0: Query tracker by PO number directly ──────────────────
                        # This is the PRIMARY check. It catches re-submitted POs that arrive
                        # as a new email (new message_id / new file) days later.
                        # Any prior record for this PO number in a non-failed state = duplicate.
                        _DUPLICATE_STATUSES = {
                            "PUSHED_TO_CELONIS", "PENDING_STAGE2", "ROBONA_SENT",
                            "SO_BLOCKED", "MAPPED_SUCCESS", "SKIP_DUPLICATE",
                        }
                        try:
                            _prev_pos = _tracker6.get_emails_by_po_number(check_po)
                            for _r in _prev_pos:
                                _other_id = _r.get("message_id") or _r.get("RowKey", "")
                                if _other_id == current_msg_id:
                                    continue  # skip the current email itself
                                _st = str(_r.get("status", "") or "").strip().upper()
                                if _st in _DUPLICATE_STATUSES:
                                    print(f"    [DEDUP] PO '{check_po}' previously processed by "
                                          f"msg {_other_id[:20]} (status={_st})")
                                    return True
                        except Exception as _e0:
                            print(f"    [WARN] PO-number dedup check failed: {_e0}")

                        # ── Check 1: Celonis SO cache ────────────────────────────────────
                        cache_file = Path(__file__).parent / ".celonis_cache" / "so_creation_results.parquet"
                        if cache_file.exists():
                            try:
                                import pandas as pd
                                df = pd.read_parquet(str(cache_file))
                                if df is not None and not df.empty:
                                    df.columns = [c.upper() for c in df.columns]
                                    if "PO_NUMBER" in df.columns and "SO_NUMBER" in df.columns:
                                        po_clean = str(check_po).strip().lower()
                                        mask = df["PO_NUMBER"].astype(str).str.strip().str.lower() == po_clean
                                        df_po = df[mask]
                                        for _, row in df_po.iterrows():
                                            so_num = str(row.get("SO_NUMBER", "") or "").strip()
                                            celonis_status = str(row.get("STATUS", "") or "").strip().upper()
                                            if so_num and celonis_status != "FAILED":
                                                return True
                            except Exception as e:
                                print(f"    [WARN] Duplicate check failed reading Celonis cache: {e}")

                        # ── Check 2: In-flight tracker check (by source_file / age) ──────
                        _in_flight = _tracker6.get_eligible_emails(
                            ["PUSHED_TO_CELONIS", "PENDING_STAGE2", "ROBONA_SENT", "SO_BLOCKED"]
                        )
                        for _r in _in_flight:
                            _other_id     = _r.get("message_id") or _r.get("RowKey", "")
                            _other_sf     = _r.get("source_file", "") or ""
                            _other_status = _r.get("status", "") or ""
                            if _other_id == current_msg_id:
                                continue
                            _other_po = str(_r.get("po_number", "") or "").strip()
                            if not _other_po:
                                _other_po = file_info_map.get(_other_sf.lower(), {}).get("po_number", "")
                            if _other_sf != check_sf and _other_po.lower() != check_po.lower():
                                continue
                            if _other_status in ("ROBONA_SENT", "SO_BLOCKED"):
                                return True
                            if _other_status in ("PUSHED_TO_CELONIS", "PENDING_STAGE2"):
                                try:
                                    from datetime import datetime
                                    _upd = _r.get("updated_at", "") or ""
                                    updated_dt = datetime.strptime(_upd[:19], "%Y-%m-%d %H:%M:%S")
                                    age_hours = (datetime.utcnow() - updated_dt).total_seconds() / 3600
                                except Exception:
                                    age_hours = 0.0
                                if age_hours < 12.0:
                                    return True
                        return False

                    if has_active_or_successful_so(po_number, source_file, msg_id):
                        print(f"    [SKIP_DUPLICATE] {source_file} — PO '{po_number}' already has a Sales Order "
                              f"or active in-flight attempt. Skipping to prevent duplicate SAP SO.")
                        _tracker6.update_status(msg_id, "SKIP_DUPLICATE",
                                                extra_fields={"po_number": str(po_number).strip().lower()})
                        continue
                    else:
                        print(f"    [DUPLICATE ALLOWED] {source_file} — PO '{po_number}' previously attempted but no Sales Order exists. Re-processing.")

                # ── STAGE 1 PASS ─────────────────────────────────────────────
                # Data is already in Celonis (pushed in Step 3.5).
                # Mark as PUSHED_TO_CELONIS so Step 7 moves it to 'Processed POs'.
                # Also persist po_number so future runs can detect duplicate resubmissions
                # via get_emails_by_po_number() even if they arrive with a new message_id.
                # Robona + Stage 2 notifications will fire from run_celonis_feedback.py
                # once the Action Flow confirms the SAP Sales Order was created.
                _extra = {}
                if po_number:
                    # Store lowercased: Azure OData has no LOWER() function, so
                    # get_emails_by_po_number() filters by exact match on lowercase value.
                    _extra["po_number"] = str(po_number).strip().lower()
                _tracker6.update_status(msg_id, "PUSHED_TO_CELONIS", extra_fields=_extra or None)
                pushed_count += 1
                label = source_file or msg_id[:20]
                print(f"    [PUSHED_TO_CELONIS] {label} — awaiting Action Flow result")

            elif status in ("PENDING", "EXTRACTION_FAILED", "MAPPING_FAILED") \
                    and msg_id not in sent_exception_ids:
                # ── STAGE 1 EXCEPTION ─────────────────────────────────────────
                # Mapping or extraction failed — pipeline cannot create SAP SO.
                # If this email had MULTIPLE POs and only some failed (multi_fail_map),
                # send ONE exception email per failing PO file so CSR gets a
                # separate notification for each issue.

                fail_files = multi_fail_map.get(msg_id, {}).get("missing", []) + \
                             multi_fail_map.get(msg_id, {}).get("failed",  [])
                if not fail_files:
                    # Single-PO email (normal case): fall back to source_file from tracker
                    fail_files = [source_file] if source_file else []

                # ── BUG FIX 1: body-only emails (e.g. amendment emails with no PDF) ──
                # If fail_files is still empty (EXTRACTION_FAILED on an email that had
                # no PDF attachment — only an HTML body with a table, like NY24409),
                # the for-loop below would never run and NO exception email would be sent.
                # Fix: use a sentinel [None] so the loop runs exactly once and sends the
                # exception with the original .msg fetched from Graph API as attachment.
                if not fail_files:
                    fail_files = [None]   # sentinel — no file, but must send exception

                attachments_to_send = get_all_message_files(msg_id, folder, db_path)

                for fail_file in fail_files:
                    fi_key = (fail_file or "").lower()
                    sf_info_f      = file_info_map.get(fi_key, {})
                    sales_org_f    = sf_info_f.get("sales_org")
                    missing_mats_f = sf_info_f.get("missing_materials", [])
                    # -- Build failure_reason -- escalation-aware ----------------------
                    _is_cust_esc = sf_info_f.get("customer_escalated", False)
                    _esc_cands   = sf_info_f.get("escalation_candidates", [])
                    _esc_score   = sf_info_f.get("escalation_score", 0.0)
                    _esc_rsn     = sf_info_f.get("escalation_reason", "")

                    if _is_cust_esc and _esc_cands:
                        _cands_str = ", ".join(str(c) for c in _esc_cands)
                        _score_pct = f"{int(_esc_score * 100)}%" if _esc_score else "low"
                        if _esc_rsn == "customer_ambiguous":
                            failure_reason = (
                                f"Customer match AMBIGUOUS - AI found {len(_esc_cands)} possible SAP "
                                f"customers and could not safely choose one automatically.\n\n"
                                f"Please reply to confirm the correct SAP Sold-To ID:\n"
                                f"  Candidate IDs    : {_cands_str}\n"
                                f"  Best match score : {_score_pct}"
                            )
                        else:
                            failure_reason = (
                                f"Customer match LOW CONFIDENCE - best candidate: {_cands_str[:80]} "
                                f"(confidence {_score_pct}, required >=85%).\n\n"
                                f"Please reply to confirm the correct SAP Sold-To ID."
                            )
                    elif _is_cust_esc:
                        failure_reason = (
                            "Customer could not be matched to a SAP Sold-To ID with sufficient confidence. "
                            "Please reply with the correct SAP Sold-To ID."
                        )
                    elif status == "MAPPING_FAILED" and missing_mats_f:
                        failure_reason = (
                            f"Missing material mapping for customer material(s): "
                            f"{', '.join(missing_mats_f)}"
                        )
                    elif status == "EXTRACTION_FAILED":
                        if not fail_file:
                            # Body-only email — no PDF, just an HTML table / amendment
                            failure_reason = (
                                "Email body could not be parsed as a Purchase Order — "
                                "no PO line items found. "
                                "This may be an amendment, acknowledgement, or forwarded email chain "
                                "that does not contain a new PO. "
                                "Please review and create the Sales Order manually if required."
                            )
                        else:
                            failure_reason = "PDF extraction failed - pipeline could not read PO data."
                    else:
                        failure_reason = f"Pipeline status: {status} - PO could not be processed automatically."

                    # Attach ONLY the failing PO's file if multiple POs in the email
                    if len(fail_files) > 1 and fail_file:
                        # Filter attachments to just the failing PO file
                        this_att = [a for a in attachments_to_send
                                    if os.path.basename(a).lower() == os.path.basename(fail_file).lower()]
                        atts_for_email = this_att if this_att else attachments_to_send
                    else:
                        atts_for_email = attachments_to_send

                    # ── BUG FIX 2: Fallback to Graph API .msg when disk search finds nothing ──
                    # Happens when:
                    #   a) The PO file was already archived before Step 6 runs (race condition)
                    #   b) The email had no PDF attachment (body-only amendment like NY24409)
                    # Fix: fetch the original email as a raw .msg from Graph API and
                    # write it to a temp file so send_exception_email() can attach it.
                    if not atts_for_email and msg_id and not is_dry_run:
                        print(f"  [ATTACH-FALLBACK] No disk file found — fetching original .msg from Graph API "
                              f"for msg_id={msg_id[:20]}...")
                        try:
                            import base64 as _b64, tempfile as _tmp
                            _token = handler._get_token()
                            _eml_url = (f"https://graph.microsoft.com/v1.0/users/"
                                        f"{handler.mailbox}/messages/{msg_id}/$value")
                            import requests as _req
                            _eml_resp = _req.get(_eml_url,
                                                 headers={"Authorization": f"Bearer {_token}",
                                                          "Accept": "application/octet-stream"},
                                                 timeout=30)
                            if _eml_resp.status_code == 200:
                                _subj_safe = (subject or fail_file or "original_email")[:40]
                                _subj_safe = "".join(c for c in _subj_safe if c.isalnum() or c in " _-")
                                _tmp_path = os.path.join(
                                    folder, f"_graph_fallback_{_subj_safe.strip()}.msg"
                                )
                                with open(_tmp_path, "wb") as _f:
                                    _f.write(_eml_resp.content)
                                atts_for_email = [_tmp_path]
                                print(f"  [ATTACH-FALLBACK] Fetched original .msg "
                                      f"({len(_eml_resp.content)//1024} KB) — attaching as '{os.path.basename(_tmp_path)}'")
                            else:
                                print(f"  [ATTACH-FALLBACK] Graph API returned {_eml_resp.status_code} "
                                      f"— sending exception without attachment")
                        except Exception as _fe:
                            print(f"  [ATTACH-FALLBACK] Failed to fetch .msg: {_fe} — sending without attachment")

                    if atts_for_email:
                        print(f"  [INFO] Attaching {len(atts_for_email)} file(s): "
                              f"{', '.join([os.path.basename(x) for x in atts_for_email])}")
                    else:
                        print(f"  [INFO] No attachment available for {fail_file or subject or msg_id[:20]}"
                              f" — sending exception email without attachment")

                    header_fields = sf_info_f.get("header_fields", {})
                    if handler.send_exception_email(
                        msg_id,
                        failure_reason,
                        sales_org=sales_org_f,
                        original_filename=fail_file or subject or "Unknown",
                        file_path=atts_for_email,
                        dry_run=is_dry_run,
                        header_fields=header_fields,
                        status=status,
                        missing_materials=missing_mats_f,
                    ):
                        exception_count += 1

                sent_exception_ids.add(msg_id)  # block duplicate for same email this run

                # Mark as EXCEPTION_ROUTED — Step 7 moves this to 'Exception POs'.
                # NOTE: Do NOT call archive_email() here — message_id must stay valid
                # for Step 7's folder move. Archiving first invalidates the Graph API ID.
                _tracker6.update_status(msg_id, "EXCEPTION_ROUTED")

        print(f"\n  Stage 1 Post-Processing Summary:")
        print(f"     Pushed to Celonis  : {pushed_count} (awaiting Stage 2 Celonis feedback)")
        print(f"     Stage 1 Exceptions : {exception_count} exception email(s) sent to CSR (1 per PO)")
        print(f"  NOTE: Robona notifications will fire via run_celonis_feedback.py")
        print(f"        once the Celonis Action Flow confirms SAP SO creation.")

    except ImportError:
        print("  [WARN] graph_email_handler not available - skipping post-processing.")
    except Exception as e:
        safe_e = str(e).encode('ascii', 'ignore').decode()
        print(f"  [WARN] Post-processing error (non-fatal): {safe_e}")


    # -- Step 7: Move emails to Processed / Exception / Unprocessed folders --
    # ─────────────────────────────────────────────────────────────────────────────
    # Uses the ORIGINAL message_id for each email (not invalidated by archive_email()).
    # Status routing:
    #   PUSHED_TO_CELONIS  -> 'Processed POs'    (mapping OK, awaiting Celonis Stage 2)
    #   EXCEPTION_ROUTED   -> 'Exception POs'    (Stage 1 failure — CSR already notified)
    #   MAPPING_FAILED     -> 'Exception POs'    (fallback if Step 6 did not update status)
    #   EXTRACTION_FAILED  -> 'Exception POs'    (fallback)
    #   SKIP               -> skip (system emails, no folder move needed)
    #   PENDING            -> 'Unprocessed POs'  (pipeline did not complete for this email)
    # ─────────────────────────────────────────────────────────────────────────────
    _skip_folder_move = getattr(args, 'dry_run', False) or getattr(args, 'no_folder_move', False)
    if _skip_folder_move:
        print("\n" + "=" * 60)
        print("  7. Moving emails to Outlook folders (Processed / Exception / Unprocessed)")
        print("=" * 60)
        _skip_reason = "--dry-run" if getattr(args, 'dry_run', False) else "--no-folder-move"
        print(f"  [SKIPPED] Outlook folder moves skipped ({_skip_reason}).")
        print(f"  Emails remain in their current folder — run without this flag to commit.")
    else:
      try:
        from setup_mailbox_folders import get_token, mark_as_processed, mark_as_unprocessed, mark_as_exception

        print("\n" + "=" * 60)
        print("  7. Moving emails to Outlook folders (Processed / Exception / Unprocessed)")
        print("=" * 60)

        token = get_token()

        # Use AzureEmailTracker so emails in Azure Table Storage are found.
        from azure_email_tracker import AzureEmailTracker as _Tracker7
        _tracker7 = _Tracker7()

        # Fetch all emails that still need a folder move (not yet in terminal state).
        _skip_statuses = ["FOLDER_MOVED", "NO_EMAIL_ID", "ARCHIVED", "PENDING_STAGE2"]
        _all_rows_raw = _tracker7.get_eligible_emails([
            "PUSHED_TO_CELONIS", "MAPPED_SUCCESS", "SO_CREATED", "ROBONA_SENT", "SO_BLOCKED",
            "EXCEPTION_ROUTED", "MAPPING_FAILED", "EXTRACTION_FAILED", "SO_CREATION_FAILED",
            "SKIP", "SKIP_DUPLICATE", "PENDING",
        ])
        rows_to_process = [
            (
                r.get("message_id") or r.get("RowKey", ""),
                r.get("status", ""),
                r.get("source_file", ""),
            )
            for r in _all_rows_raw
        ]

        moved_processed   = 0
        moved_exception   = 0
        moved_unprocessed = 0
        skipped           = 0
        no_id_count       = 0
        already_moved     = 0

        def _is_valid_outlook_id(mid: str) -> bool:
            """Real Outlook Graph API message IDs start with AAMk or AQMk and are long."""
            if not mid:
                return False
            mid_stripped = mid.strip()
            return (
                (mid_stripped.startswith("AAMk") or mid_stripped.startswith("AQMk"))
                and len(mid_stripped) > 40
            )

        for msg_id, status, source_file in rows_to_process:
            label = source_file or msg_id[:30]

            # ── Guard: skip if no valid Outlook message ID ──────────────────
            # Azure Blob files and locally-renamed files never have an Outlook ID.
            # Their "message_id" in the tracker is actually the filename. Skip silently
            # and mark as NO_EMAIL_ID so we never retry them.
            if not _is_valid_outlook_id(msg_id):
                _tracker7.update_status(msg_id, "NO_EMAIL_ID")
                no_id_count += 1
                continue  # no print spam — these are Azure-only files

            if status == "SKIP":
                # SKIP = non-PO email (no attachment, system email, etc.)
                # Move to Processed POs so it doesn't sit in Unprocessed POs indefinitely.
                ok = mark_as_processed(token, msg_id, silent=True)
                if ok:
                    moved_processed += 1
                    print(f"    [SKIP->PROCESSED] '{label}' -> Processed POs (non-PO email, no action needed)")
                else:
                    already_moved += 1
                _tracker7.update_status(msg_id, "FOLDER_MOVED")

            elif status in ("PUSHED_TO_CELONIS", "MAPPED_SUCCESS", "SO_CREATED", "ROBONA_SENT", "SO_BLOCKED"):
                ok = mark_as_processed(token, msg_id, silent=True)
                if ok:
                    moved_processed += 1
                    print(f"    [PROCESSED] '{label}' -> Processed POs")
                else:
                    # 404 = already moved in a prior run — treat as done
                    already_moved += 1
                _tracker7.update_status(msg_id, "PENDING_STAGE2")

            elif status in ("EXCEPTION_ROUTED", "MAPPING_FAILED",
                            "EXTRACTION_FAILED", "SO_CREATION_FAILED"):
                ok = mark_as_exception(token, msg_id, silent=True)
                if ok:
                    moved_exception += 1
                    print(f"    [EXCEPTION] '{label}' -> Exception POs")
                else:
                    already_moved += 1
                _tracker7.update_status(msg_id, "FOLDER_MOVED")

            elif status == "SKIP_DUPLICATE":
                # Duplicate PO — already processed from another email.
                # Move to 'Processed POs' so it doesn't sit in Unprocessed,
                # then mark FOLDER_MOVED so it is never retried.
                ok = mark_as_processed(token, msg_id, silent=True)
                if ok:
                    moved_processed += 1
                    print(f"    [SKIP_DUPLICATE] '{label}' -> Processed POs (duplicate PO — original already sent)")
                else:
                    already_moved += 1
                _tracker7.update_status(msg_id, "FOLDER_MOVED")

            elif status == "PENDING":
                ok = mark_as_unprocessed(token, msg_id, silent=True)
                if ok:
                    moved_unprocessed += 1
                    print(f"    [UNPROCESSED] '{label}' -> Unprocessed POs")
                # PENDING stays PENDING if move fails — will retry next run

            else:
                print(f"    [UNKNOWN STATUS] '{label}' status='{status}' - skipping")

        print(f"\n  Folder Move Summary:")
        print(f"     -> 'Processed POs'      : {moved_processed} email(s) moved now")
        print(f"     -> 'Exception POs'      : {moved_exception} email(s) moved now")
        print(f"     -> 'Unprocessed POs'    : {moved_unprocessed} email(s) moved now")
        print(f"     -> Already in folder    : {already_moved} (moved in a prior run, marked done)")
        print(f"     -> No Outlook ID (Azure): {no_id_count} (Azure-only files, no email to move)")
        print(f"     -> Skipped (system)     : {skipped} email(s)")

      except Exception as e:
        print(f"  [WARN] Step 7 folder move error: {e}")



    # -- Step 8: Archive processed local files --
    if folder.name == "outlook_po_extracted" or not args.skip_outlook:
        try:
            print("\n" + "=" * 60)
            print("  8. Archiving processed local files to 'outlook_po_archive'")
            print("=" * 60)
            archive_dir = folder.parent / "outlook_po_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            
            archived_count = 0
            for item in folder.iterdir():
                if item.is_file():
                    dest = archive_dir / item.name
                    # If destination file already exists in archive, overwrite it safely
                    if dest.exists():
                        try:
                            dest.unlink()
                        except Exception:
                            pass
                    try:
                        item.rename(dest)
                        archived_count += 1
                    except Exception:
                        # Fallback to copy + delete if rename fails across drives/volumes
                        try:
                            import shutil
                            shutil.copy2(str(item), str(dest))
                            item.unlink()
                            archived_count += 1
                        except Exception:
                            pass
            if archived_count > 0:
                print(f"  [Archive] Successfully archived {archived_count} files to: {archive_dir}")
            else:
                print("  [Archive] No local files to archive.")
                
            # Note: Celonis Parquet cache files are managed automatically by azure_table_reader.py
            # and will refresh every 24 hours. Delete the '.celonis_cache' folder to force a manual reload.
            pass
        except Exception as e:
            print(f"  [WARN] Local archiving/cache clearing failed (non-fatal): {e}")

    # -- Flush audit log (local JSONL + Azure Blob) --
    try:
        get_audit_logger().flush()
    except Exception as _al_err:
        print(f"  [Audit-WARN] Audit log flush failed (non-fatal): {_al_err}")

    print("\n" + "=" * 60)
    print("  PIPELINE COMPLETED")
    print(f"  Input folder: {folder}")
    print(f"  Output: extracted_pos_full.csv")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
