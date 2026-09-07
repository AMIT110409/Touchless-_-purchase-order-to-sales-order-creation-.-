import sqlite3
conn = sqlite3.connect("processed_emails.db")
c = conn.cursor()
c.execute("UPDATE processed_emails SET status='PENDING', updated_at=CURRENT_TIMESTAMP")
print(f"Reset {c.rowcount} rows to PENDING")
conn.commit()
conn.close()
