import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import app as app_module

conn = app_module.get_db_connection()
user = conn.execute('SELECT id, username FROM users ORDER BY id LIMIT 1').fetchone()
conn.close()

if user is None:
    print('NO_USER')
    raise SystemExit(0)

app = app_module.app
app.testing = True
client = app.test_client()

with client.session_transaction() as sess:
    sess['user_id'] = user['id']
    sess['username'] = user['username']

month_resp = client.get('/dashboard?filter_type=month&filter_value=2024-11')
day_resp = client.get('/dashboard?filter_type=day&filter_value=2024-11-22')

print('user', user['id'], user['username'])
print('month_status', month_resp.status_code)
print('day_status', day_resp.status_code)
print('month_selected', '2024-11' in month_resp.get_data(as_text=True))
print('day_selected', '2024-11-22' in day_resp.get_data(as_text=True))
