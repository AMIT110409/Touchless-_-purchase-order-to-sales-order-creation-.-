import subprocess
import sys
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'
os.environ['PYTHONIOENCODING'] = 'utf-8'   # Force UTF-8 output in all subprocesses
os.environ['PYTHONUTF8'] = '1'             # Python 3.7+ UTF-8 mode for subprocesses

import argparse

def run_step(command, step_name, optional=False):
    print(f"\n{'='*60}")
    print(f"STEP: {step_name}")
    print(f"Command: {command}")
    print(f"{'='*60}")

    result = subprocess.run(command, shell=True)
    if result.returncode != 0:
        if optional:
            print(f"\nWARNING: Optional step '{step_name}' failed (exit {result.returncode}). Continuing.")
        else:
            print(f"\nERROR: Step '{step_name}' failed with exit code {result.returncode}")
            sys.exit(result.returncode)
    print(f"\nSUCCESS: {step_name} completed.")

def main():
    parser = argparse.ArgumentParser(description="End-to-End PO Extraction Pipeline")
    parser.add_argument("--folder",                  type=str, default="PO examples",
                        help="Folder containing PDF files")
    parser.add_argument("--refresh-celonis-tables",  action="store_true",
                        help="Pull fresh Sheet 2 + Sheet 3 from Celonis → Azure Blob before running")
    parser.add_argument("--use-azure-mapper",        action="store_true",
                        help="Use Azure Blob + Decision Tree for order type mapping (instead of local Excel)")
    args = parser.parse_args()

    # Define file paths
    input_folder    = args.folder
    folder_basename = os.path.basename(os.path.normpath(input_folder))
    if not folder_basename: folder_basename = "default"

    raw_output      = f"results_{folder_basename}.jsonl"
    enriched_output = f"results_{folder_basename}_enriched.jsonl"
    csv_output      = f"extracted_pos_{folder_basename}.csv"
    python_exe      = sys.executable

    # ── Step 0 (optional): Refresh Celonis tables → Azure Blob ──────────────
    if args.refresh_celonis_tables:
        cmd_refresh = f'"{python_exe}" celonis_to_azure.py'
        run_step(cmd_refresh, "0. Celonis → Azure Blob (Sheet 2 + Sheet 3)", optional=False)
    else:
        print("\n[INFO] Skipping Celonis refresh. Use --refresh-celonis-tables to pull fresh data.")
        print("       Tables will be read from local .celonis_cache/ if available.")

    # ── Step 1: Extraction ───────────────────────────────────────────────────
    cmd_extract = f'"{python_exe}" po_extraction_enhanced.py --folder "{input_folder}" --output "{raw_output}"'
    run_step(cmd_extract, "1. PDF Extraction & OCR")

    # ── Step 2: Enrichment ───────────────────────────────────────────────────
    azure_mapper_flag = "--use-azure-mapper" if args.use_azure_mapper else ""
    cmd_enrich = (
        f'"{python_exe}" reenrich_results.py '
        f'--input "{raw_output}" --output "{enriched_output}" {azure_mapper_flag}'
    )
    run_step(cmd_enrich, "2. KB Enrichment & Order Mapping (Decision Tree)")

    # ── Step 3: CSV Export ───────────────────────────────────────────────────
    cmd_export = f'"{python_exe}" run_test_export.py --input "{enriched_output}" --output "{csv_output}"'
    run_step(cmd_export, "3. CSV Export generation")

    # ── Step 4: Celonis Push ─────────────────────────────────────────────────
    cmd_celonis = f'"{python_exe}" push_to_celonis.py --input "{csv_output}"'
    run_step(cmd_celonis, "4. Push to Celonis (Append Only)")

    print("\n" + "="*60)
    print("PIPELINE COMPLETED SUCCESSFULLY!")
    print(f"Check {csv_output} for the final mapped data.")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()

