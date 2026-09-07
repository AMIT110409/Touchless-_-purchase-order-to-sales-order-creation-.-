import pandas as pd
import os

def check_excel():
    excel_path = "EXPORT_20260216_100124.XLSX"
    if not os.path.exists(excel_path):
        print(f"Error: {excel_path} not found")
        return

    df = pd.read_excel(excel_path)
    print(f"Excel Columns: {df.columns.tolist()}")
    
    # Check for specific customer from previous logs
    cust_id = "4020032023"
    print(f"\n--- Checking data for Customer: {cust_id} ---")
    
    # Try dynamic column detection similar to mapper
    col_cust = next((c for c in df.columns if 'customer' in c.lower()), None)
    col_mat = next((c for c in df.columns if 'material' in c.lower() and 'description' not in c.lower() and 'customer' not in c.lower()), None)
    col_cust_mat = next((c for c in df.columns if 'customer' in c.lower() and 'material' in c.lower()), None)
    
    print(f"Detected Columns: Customer='{col_cust}', Material='{col_mat}', Cust_Material='{col_cust_mat}'")
    
    if col_cust:
        cust_data = df[df[col_cust].astype(str).str.contains(cust_id, na=False)]
        print(f"Count of rows for this customer: {len(cust_data)}")
        if not cust_data.empty:
            print("First 5 rows for this customer:")
            cols_to_show = [col_cust, col_mat]
            if col_cust_mat: cols_to_show.append(col_cust_mat)
            if 'Material Description' in df.columns: cols_to_show.append('Material Description')
            print(cust_data[cols_to_show].head().to_string())
        else:
            print("No rows found for this customer ID in Excel.")

if __name__ == "__main__":
    check_excel()
