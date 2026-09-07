import os, sqlite3
from dotenv import load_dotenv
from outlook_poller import get_access_token, _graph_request

load_dotenv()
user = os.getenv('TARGET_EMAIL_USER') or 'salesorders@envalior.com'
token = get_access_token()
headers = {'Authorization': f'Bearer {token}'}

# Get Unprocessed POs folder
url = f'https://graph.microsoft.com/v1.0/users/{user}/mailFolders'
res = _graph_request('GET', url, headers=headers)
folder_id = None
for f in res.json().get('value', []):
    dname = f.get('displayName', '').strip().lower()
    if dname == 'unprocessed pos':
        folder_id = f.get('id')
        print(f"Folder 'Unprocessed POs' total: {f.get('totalItemCount')}")
        break

if not folder_id:
    print("ERROR: 'Unprocessed POs' folder not found!")
    exit(1)

msg_url = f'https://graph.microsoft.com/v1.0/users/{user}/mailFolders/{folder_id}/messages?$top=50'
mres = _graph_request('GET', msg_url, headers=headers)
msgs = mres.json().get('value', [])
print(f'Messages currently in Unprocessed POs: {len(msgs)}')
print()

for m in msgs:
    mid = m.get('id')
    subj = m.get('subject')
    sender = m.get('from', {}).get('emailAddress', {}).get('address')
    has_att = m.get('hasAttachments')
    print(f'  MID: {mid}')
    print(f'  Subject: {repr(subj)}')
    print(f'  From: {sender} | HasAttachments: {has_att}')

    # Check DB status
    conn = sqlite3.connect('processed_emails.db')
    cur = conn.cursor()
    cur.execute('SELECT status, stage, attachments, error_log FROM processed_emails WHERE message_id=?', (mid,))
    row = cur.fetchone()
    conn.close()
    if row:
        print(f'  DB Status: {row[0]} | Stage: {row[1]}')
        print(f'  DB Attachments: {str(row[2] or "")[:80]}')
        print(f'  DB Error: {str(row[3] or "")[:100]}')
    else:
        print(f'  DB Status: NOT IN DB (never tracked)')

    # Get attachments from Graph API
    att_url = f'https://graph.microsoft.com/v1.0/users/{user}/messages/{mid}/attachments'
    ares = _graph_request('GET', att_url, headers=headers)
    if ares.status_code == 200:
        atts = ares.json().get('value', [])
        print(f'  Attachments from Graph ({len(atts)}):')
        for a in atts:
            aname = a.get('name', '')
            atype = a.get('@odata.type', '')
            ainline = a.get('isInline')
            asize = a.get('size')
            print(f'    - name={repr(aname)} | type={atype} | inline={ainline} | size={asize}b')
    print()
