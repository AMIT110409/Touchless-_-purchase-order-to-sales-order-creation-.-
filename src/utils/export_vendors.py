import sqlite3

db_path = "knowledge_base.db"
output_file = "vendor_list.txt"

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get distinct names, sorted
    cursor.execute("SELECT DISTINCT name FROM vendors WHERE name IS NOT NULL ORDER BY name")
    rows = cursor.fetchall()
    
    with open(output_file, "w", encoding="utf-8") as f:
        for r in rows:
            if r[0]:
                f.write(f"{r[0]}\n")
                
    print(f"Success! {len(rows)} vendors written to {output_file}")
    conn.close()

except Exception as e:
    print(f"Error: {e}")
