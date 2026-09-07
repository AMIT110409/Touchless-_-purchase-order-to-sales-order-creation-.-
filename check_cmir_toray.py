"""
Direct CMIR search for Q150E B-MB and TORAY customer material numbers.
"""
import sys, os, pandas as pd
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'
from dotenv import load_dotenv
load_dotenv()

cache = Path('.celonis_cache')
files = list(cache.glob('*.parquet')) + list(cache.glob('*.csv'))
print(f"Cache files found: {[f.name for f in files]}")

for cf in files:
    try:
        df = pd.read_parquet(str(cf)) if str(cf).endswith('.parquet') else pd.read_csv(str(cf))
        orig_cols = list(df.columns)
        df.columns = [c.upper().strip() for c in df.columns]
        print(f"\n=== {cf.name} — columns: {list(df.columns)[:15]} ===")
        print(f"  Rows: {len(df)}")

        # Search specifically for Q150 in any column
        mask_q150 = df.apply(lambda col: col.astype(str).str.contains('Q150', case=False, na=False)).any(axis=1)
        if mask_q150.any():
            print(f"\n  Q150 rows ({mask_q150.sum()}):")
            print(df[mask_q150].to_string(max_colwidth=35))
        else:
            print("  No Q150 rows found.")

        # Search for TORAY sold-to IDs
        for sid in ['4020000084', '4020000085']:
            mask_st = df.apply(lambda col: col.astype(str).str.contains(sid, case=False, na=False)).any(axis=1)
            if mask_st.any():
                print(f"\n  SoldTo {sid} rows ({mask_st.sum()}):")
                print(df[mask_st].head(5).to_string(max_colwidth=40))
    except Exception as e:
        print(f"Error reading {cf}: {e}")
