import pandas as pd
import os

def diagnose():
    excel_file = "EXPORT_20260216_100124.XLSX"
    if not os.path.exists(excel_file):
        print(f"Error: {excel_file} not found")
        return

    print(f"Loading {excel_file}...")
    df = pd.read_excel(excel_file, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    targets = ["11110201", "F 132-E1", "111102"]

    for target in targets:
        print(f"\n--- Searching for: {target} ---")
        mask1 = df['Material'].astype(str).str.contains(target, case=False, na=False)
        mask2 = df['Customer Material Number'].astype(str).str.contains(target, case=False, na=False)
        mask3 = pd.Series([False] * len(df))
        if 'Material Description' in df.columns:
            mask3 = df['Material Description'].astype(str).str.contains(target, case=False, na=False)
        
        matches = df[mask1 | mask2 | mask3]
        
        if matches.empty:
            print(f"  [!] NOT FOUND in Excel.")
        else:
            print(f"  [+] Found {len(matches)} matches:")
            for _, row in matches.iterrows():
                print(f"    Customer: {row.get('Customer', '???')} | Material: {row.get('Material', '???')} | Cust Mat: {row.get('Customer Material Number', '???')} | SalesOrg: {row.get('Sales Organization', '???')}")

if __name__ == "__main__":
    diagnose()
