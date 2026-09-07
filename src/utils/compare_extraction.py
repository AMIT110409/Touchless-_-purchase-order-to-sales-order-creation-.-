"""
compare_extraction.py
---------------------
Side-by-side accuracy comparison between:
  - Original: results_paddle.jsonl  (PaddleOCR + free-form prompt)
  - LangExtract: results_langextract.jsonl  (PaddleOCR + Pydantic schema)

Output: comparison_report.xlsx  with per-file, per-field comparison.
"""

import json
import pandas as pd
from pathlib import Path

# -- Fields to compare --------------------------
HEADER_FIELDS = [
    "po_number",
    "order_date",
    "vendor_name",
    "customer_id_or_name",
    "ship_to_address",
]
ITEM_FIELDS = ["material_code", "quantity", "unit"]

# -- Load JSONL ----------------------------------
def load_jsonl(path):
    records = {}
    if not Path(path).exists():
        print(f"File not found: {path}")
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                src = Path(r.get("source_file", "")).stem
                records[src] = r
            except:
                pass
    return records

def val(v):
    """Normalize value for display."""
    if v is None or str(v).strip() == "" or str(v).lower() == "null":
        return "—"
    return str(v).strip()[:60]

def has_value(v):
    return v is not None and str(v).strip() not in ("", "null", "None")

def main():
    orig = load_jsonl("results_paddle.jsonl")
    lang = load_jsonl("results_langextract.jsonl")

    all_files = sorted(set(list(orig.keys()) + list(lang.keys())))

    if not all_files:
        print("No records found in either file.")
        return

    rows = []
    summary = {"file": [], "original_filled": [], "langextract_filled": [], "winner": []}

    for src in all_files:
        o = orig.get(src, {})
        l = lang.get(src, {})

        oh = o.get("header_fields") or {}
        lh = l.get("header_fields") or {}

        # -- Header field comparison --
        for field in HEADER_FIELDS:
            ov = oh.get(field)
            lv = lh.get(field)
            rows.append({
                "File": src,
                "Level": "Header",
                "Field": field,
                "Original (Paddle)": val(ov),
                "LangExtract": val(lv),
                "Original has value": "✅" if has_value(ov) else "❌",
                "LangExtract has value": "✅" if has_value(lv) else "❌",
                "Better": (
                    "LangExtract" if has_value(lv) and not has_value(ov)
                    else "Original" if has_value(ov) and not has_value(lv)
                    else "Same" if has_value(ov) == has_value(lv)
                    else "—"
                )
            })

        # -- Line items comparison (first item only for summary) --
        oi = (o.get("line_items") or [{}])[0] if o.get("line_items") else {}
        li = (l.get("line_items") or [{}])[0] if l.get("line_items") else {}

        for field in ITEM_FIELDS:
            ov = oi.get(field)
            lv = li.get(field)
            rows.append({
                "File": src,
                "Level": "Line Item 1",
                "Field": field,
                "Original (Paddle)": val(ov),
                "LangExtract": val(lv),
                "Original has value": "✅" if has_value(ov) else "❌",
                "LangExtract has value": "✅" if has_value(lv) else "❌",
                "Better": (
                    "LangExtract" if has_value(lv) and not has_value(ov)
                    else "Original" if has_value(ov) and not has_value(lv)
                    else "Same" if has_value(ov) == has_value(lv)
                    else "—"
                )
            })

        # -- Per-file summary --
        all_fields = HEADER_FIELDS + ITEM_FIELDS
        o_filled = sum(1 for f in HEADER_FIELDS if has_value(oh.get(f)))
        o_filled += sum(1 for f in ITEM_FIELDS if has_value(oi.get(f)))
        l_filled = sum(1 for f in HEADER_FIELDS if has_value(lh.get(f)))
        l_filled += sum(1 for f in ITEM_FIELDS if has_value(li.get(f)))
        total = len(all_fields)

        summary["file"].append(src)
        summary["original_filled"].append(f"{o_filled}/{total}")
        summary["langextract_filled"].append(f"{l_filled}/{total}")
        summary["winner"].append(
            "LangExtract" if l_filled > o_filled
            else "Original" if o_filled > l_filled
            else "Tie"
        )

    df_detail = pd.DataFrame(rows)
    df_summary = pd.DataFrame(summary)

    # -- Summary stats --
    orig_total = sum(1 for r in rows if r["Original has value"] == "✅")
    lang_total = sum(1 for r in rows if r["LangExtract has value"] == "✅")
    total_fields = len(rows)

    print("\n" + "="*60)
    print("    EXTRACTION ACCURACY COMPARISON")
    print("="*60)
    print(f"  Files compared:    {len(all_files)}")
    print(f"  Original filled:   {orig_total}/{total_fields} ({100*orig_total//total_fields}%)")
    print(f"  LangExtract filled:{lang_total}/{total_fields} ({100*lang_total//total_fields}%)")
    wins = sum(1 for w in summary["winner"] if w == "LangExtract")
    orig_wins = sum(1 for w in summary["winner"] if w == "Original")
    ties = sum(1 for w in summary["winner"] if w == "Tie")
    print(f"\n  LangExtract better in: {wins} files")
    print(f"  Original better in:    {orig_wins} files")
    print(f"  Ties:                  {ties} files")
    print("="*60)

    # -- Save Excel --
    out = "comparison_report.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_summary.to_excel(writer, sheet_name="Summary", index=False)
        df_detail.to_excel(writer, sheet_name="Field Detail", index=False)

    print(f"\nReport saved to: {out}")
    import subprocess, sys
    subprocess.Popen(["start", out], shell=True)

if __name__ == "__main__":
    main()
