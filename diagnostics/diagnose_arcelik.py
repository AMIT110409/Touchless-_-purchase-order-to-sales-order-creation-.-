import pandas as pd, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from difflib import SequenceMatcher
import unicodedata

def norm(text):
    nfkd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn").lower().strip()

df = pd.read_parquet(".celonis_cache/test_mp_customer_master.parquet")

# ARCELİK
print("=== ARCELIK ===")
extracted = "ARCELiK A.S Eskisehir Kompresor I."  # normalized version
mask = df["sold_to_name_full"].str.contains("Arce", case=False, na=False)
hits = df[mask][["sold_to_id","sold_to_name_full"]].drop_duplicates("sold_to_id")
for _, row in hits.iterrows():
    v_norm = norm(row["sold_to_name_full"])
    e_norm = norm(extracted)
    score = SequenceMatcher(None, e_norm, v_norm).ratio()
    print(f"  {row['sold_to_id']} | {v_norm[:50]} | score={score:.3f} | pass={score>=0.65}")

print()
print("=== CEGAN ===")
mask = df["sold_to_name_full"].str.contains("egan", case=False, na=False)
hits = df[mask][["sold_to_id","sold_to_name_full"]].drop_duplicates("sold_to_id")
for _, row in hits.iterrows():
    v_norm = norm(row["sold_to_name_full"])
    e_norm = norm("CEGAN s.r.o.")
    score = SequenceMatcher(None, e_norm, v_norm).ratio()
    prefilter = e_norm[:3] in v_norm
    print(f"  {row['sold_to_id']} | {row['sold_to_name_full'][:50]} | norm={v_norm[:30]} | prefilter={prefilter} | score={score:.3f}")
