import sqlite3
import os

def check_db():
    db_path = "knowledge_base.db"
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    ids = ['4020001301', '4020000825', '4020010339', '4020010375', '4020013022']
    
    print(f"{'ID':<12} | {'Name'}")
    print("-" * 50)
    
    for cid in ids:
        cursor.execute("SELECT name FROM vendors WHERE customer_number = ? LIMIT 1", (cid,))
        row = cursor.fetchone()
        name = row[0] if row else "NOT FOUND"
        print(f"{cid:<12} | {name}")
    
    conn.close()

if __name__ == "__main__":
    check_db()
