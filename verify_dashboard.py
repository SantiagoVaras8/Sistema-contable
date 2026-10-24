import os
import shutil
import sys
import tempfile
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import app as app_module

conn = app_module.get_db_connection()
user = conn.execute('SELECT id, username FROM users ORDER BY id LIMIT 1').fetchone()
if user is None:
    user_id = 1
    username = 'Prueba'
else:
    user_id = user['id']
    username = user['username']

income_totals = conn.execute(
    '''
    SELECT
        COALESCE(SUM(amount_bruto), 0.0) AS total_bruto,
        COALESCE(SUM(amount_neto), 0.0) AS total_neto
    FROM incomes
    WHERE user_id = ? AND substr(income_date, 1, 7) = ?
    ''',
    (user_id, datetime.now().strftime('%Y-%m')),
).fetchone()
conn.close()

app = app_module.app
app.testing = True
client = app.test_client()

with client.session_transaction() as sess:
    sess['user_id'] = user_id
    sess['username'] = username

response = client.get('/dashboard')
page = response.get_data(as_text=True)

assert response.status_code == 200
assert 'Resumen de ingresos' in page
assert 'Ingresos brutos' in page
assert 'Ingresos netos' in page
assert 'Saldo total pendiente' in page
assert 'Pagos más recientes' in page
assert 'type="month"' not in page
assert '<table' not in page
assert f"$ {income_totals['total_bruto']:.2f}" in page
assert f"$ {income_totals['total_neto']:.2f}" in page
assert 'Total del mes actual' in page

income_response = client.get('/ingresos/nuevo')
income_page = income_response.get_data(as_text=True)
assert income_response.status_code == 200
assert 'Historial de ingresos' in income_page
assert 'Ingreso bruto' in income_page
assert 'Ingreso neto' in income_page
assert 'name="filter_type"' in income_page
assert 'name="filter_value"' in income_page
assert 'type="month"' in income_page
assert "valueInput.type = isMonth ? 'month' : 'date';" in income_page
assert 'id="confirm-dialog"' in income_page

login_error = client.post(
    '/login',
    data={'username': 'usuario-inexistente', 'password': 'incorrecta'},
)
login_error_page = login_error.get_data(as_text=True)
assert login_error.status_code == 200
assert 'flash-error' in login_error_page
assert 'Credenciales incorrectas.' in login_error_page

with client.session_transaction() as sess:
    sess['_flashes'] = [('success', 'Operación completada.')]
success_page = client.get('/dashboard').get_data(as_text=True)
assert 'flash-success' in success_page
assert 'Operación completada.' in success_page

conn = app_module.get_db_connection()
latest_income = conn.execute(
    'SELECT income_date FROM incomes WHERE user_id = ? ORDER BY income_date DESC, id DESC LIMIT 1',
    (user_id,),
).fetchone()
if latest_income:
    latest_day = latest_income['income_date']
    latest_month = latest_day[:7]
    for filter_kind, filter_value in [('day', latest_day), ('month', latest_month)]:
        query, params = app_module.get_income_filters(
            user_id,
            selected_day=filter_value if filter_kind == 'day' else None,
            selected_month=filter_value if filter_kind == 'month' else None,
        )
        expected_ids = {
            row['id'] for row in conn.execute(query, params).fetchall()
        }
        filtered_response = client.get(
            f'/ingresos/nuevo?filter_type={filter_kind}&filter_value={filter_value}'
        )
        filtered_page = filtered_response.get_data(as_text=True)
        assert filtered_response.status_code == 200
        expected_input_type = 'type="date"' if filter_kind == 'day' else 'type="month"'
        assert expected_input_type in filtered_page
        for income_id in expected_ids:
            row = conn.execute(
                'SELECT income_date, amount_bruto, tax_amount, amount_neto FROM incomes WHERE id = ?',
                (income_id,),
            ).fetchone()
            assert row['income_date'] in filtered_page
            assert f"$ {row['amount_bruto']:.2f}" in filtered_page
            assert f"$ {row['tax_amount']:.2f}" in filtered_page
            assert f"$ {row['amount_neto']:.2f}" in filtered_page
        if not expected_ids:
            assert 'No hay ingresos registrados con estos filtros.' in filtered_page
    default_query, default_params = app_module.get_income_filters(user_id)
    default_ids = {row['id'] for row in conn.execute(default_query, default_params).fetchall()}
    default_page = client.get('/ingresos/nuevo').get_data(as_text=True)
    for income_id in default_ids:
        row = conn.execute(
            'SELECT income_date, amount_bruto FROM incomes WHERE id = ?',
            (income_id,),
        ).fetchone()
        assert row['income_date'] in default_page
        assert f"$ {row['amount_bruto']:.2f}" in default_page

conn.close()

temporary_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
temporary_db.close()
shutil.copy2(app_module.DB_PATH, temporary_db.name)
original_db_path = app_module.DB_PATH
try:
    app_module.DB_PATH = temporary_db.name
    income_date = datetime.now().strftime('%Y-%m-%d')
    for tax_rate, expected_net in [(50, 100 / 3), (100, 50), (200, 200 / 3)]:
        amount = 100
        saved_response = client.post(
            '/ingresos/nuevo',
            data={
                'amount_bruto': str(amount),
                'tax_rate': str(tax_rate),
                'income_date': income_date,
            },
            follow_redirects=True,
        )
        saved_page = saved_response.get_data(as_text=True)
        expected_cost = amount - expected_net
        assert saved_response.status_code == 200
        assert saved_response.request.args == {}
        assert income_date in saved_page
        assert f"$ {amount:.2f}" in saved_page
        assert f"$ {expected_cost:.2f}" in saved_page
        assert f"$ {expected_net:.2f}" in saved_page

        conn = app_module.get_db_connection()
        saved_income = conn.execute(
            'SELECT tax_amount, amount_neto FROM incomes WHERE user_id = ? ORDER BY id DESC LIMIT 1',
            (user_id,),
        ).fetchone()
        conn.close()
        assert abs(saved_income['tax_amount'] - expected_cost) < 0.005
        assert abs(saved_income['amount_neto'] - expected_net) < 0.005
finally:
    app_module.DB_PATH = original_db_path
    os.remove(temporary_db.name)

print('Dashboard summary verified')
print('Income history and filters verified')
print('New income registration verified')
