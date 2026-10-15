from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "sistema-contable-secret")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads", "invoices")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB máximo
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp"}

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def update_provider_totals(conn, provider_id):
    pass


def column_exists(conn, table_name, column_name):
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(column[1] == column_name for column in columns)


def migrate_incomes_schema(conn):
    columns = conn.execute("PRAGMA table_info(incomes)").fetchall()
    if not columns:
        return

    column_names = [column[1] for column in columns]
    if "concept" not in column_names and "amount_bruto" in column_names and "income_date" in column_names:
        return

    conn.execute("ALTER TABLE incomes RENAME TO incomes_old")
    conn.execute(
        """
        CREATE TABLE incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount_bruto REAL NOT NULL,
            tax_rate REAL NOT NULL DEFAULT 0.0,
            tax_amount REAL NOT NULL DEFAULT 0.0,
            amount_neto REAL NOT NULL,
            income_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO incomes (id, user_id, amount_bruto, tax_rate, tax_amount, amount_neto, income_date, created_at)
        SELECT id, user_id, amount_bruto, tax_rate, tax_amount, amount_neto, income_date, created_at
        FROM incomes_old
        """
    )
    conn.execute("DROP TABLE incomes_old")


def cleanup_duplicate_provider_movements(conn):
    conn.execute(
        """
        DELETE FROM provider_movements
        WHERE movement_type = 'payment'
        """
    )
    conn.execute(
        """
        DELETE FROM provider_movements
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM provider_movements
            GROUP BY provider_id, movement_type, amount, movement_date
        )
        """
    )


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount_bruto REAL NOT NULL,
            tax_rate REAL NOT NULL DEFAULT 0.0,
            tax_amount REAL NOT NULL DEFAULT 0.0,
            amount_neto REAL NOT NULL,
            income_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    migrate_incomes_schema(conn)

    for column_name in ["amount_bruto", "tax_rate", "tax_amount", "amount_neto", "income_date"]:
        if not column_exists(conn, "incomes", column_name):
            conn.execute(f"ALTER TABLE incomes ADD COLUMN {column_name} REAL")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            balance REAL NOT NULL,
            amount_paid REAL NOT NULL DEFAULT 0.0,
            remaining_balance REAL NOT NULL,
            last_balance_date TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    if not column_exists(conn, "providers", "last_balance_date"):
        conn.execute("ALTER TABLE providers ADD COLUMN last_balance_date TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            amount_paid REAL NOT NULL,
            payment_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            movement_type TEXT NOT NULL,
            amount REAL NOT NULL,
            movement_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider_id INTEGER NOT NULL,
            invoice_number TEXT,
            amount REAL NOT NULL DEFAULT 0.0,
            invoice_date TEXT NOT NULL,
            filename TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
        """
    )
    cleanup_duplicate_provider_movements(conn)
    conn.commit()
    conn.close()


init_db()


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Debes iniciar sesión para continuar.")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


def get_user_by_id(user_id):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def normalize_provider_name(name):
    return " ".join((name or "").strip().split())


def get_provider_history(user_id, selected_day=None, selected_month=None):
    conn = get_db_connection()
    query = """
        SELECT * FROM (
            SELECT
                pm.id AS movement_id,
                p.name,
                pm.movement_type AS movement_type,
                pm.amount AS amount,
                pm.movement_date AS movement_date,
                pm.created_at AS created_at
            FROM provider_movements pm
            JOIN providers p ON p.id = pm.provider_id
            WHERE p.user_id = ? AND pm.movement_type != 'payment'
            UNION ALL
            SELECT
                pp.id AS movement_id,
                p.name,
                'payment' AS movement_type,
                pp.amount_paid AS amount,
                pp.payment_date AS movement_date,
                pp.created_at AS created_at
            FROM provider_payments pp
            JOIN providers p ON p.id = pp.provider_id
            WHERE p.user_id = ?
        )
        WHERE 1=1
    """
    params = [user_id, user_id]

    if selected_day:
        query += " AND movement_date = ?"
        params.append(selected_day)
    elif selected_month:
        query += " AND substr(movement_date, 1, 7) = ?"
        params.append(selected_month)

    query += " ORDER BY movement_date DESC, created_at DESC, movement_id DESC"

    history = conn.execute(query, params).fetchall()
    conn.close()
    return history


def get_income_filters(user_id, selected_day=None, selected_month=None):
    query = "SELECT * FROM incomes WHERE user_id = ?"
    params = [user_id]

    if selected_day:
        query += " AND income_date = ?"
        params.append(selected_day)
    elif selected_month:
        query += " AND substr(income_date, 1, 7) = ?"
        params.append(selected_month)

    query += " ORDER BY income_date DESC, id DESC"
    return query, params


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip()
        password = request.form["password"]

        if not username or not email or not password:
            flash("Completa todos los campos.")
            return render_template("register.html")

        conn = get_db_connection()
        existing_user = conn.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (username, email),
        ).fetchone()

        if existing_user:
            conn.close()
            flash("El usuario o correo ya existen.")
            return render_template("register.html")

        password_hash = generate_password_hash(password)
        conn.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (username, email, password_hash),
        )
        conn.commit()
        user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        conn.close()

        session["user_id"] = user_id
        session["username"] = username
        flash("Cuenta creada con éxito.")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            flash("Sesión iniciada correctamente.")
            return redirect(url_for("dashboard"))

        flash("Credenciales incorrectas.")
        return render_template("login.html")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Has cerrado sesión.")
    return redirect(url_for("login"))


@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    user_id = session["user_id"]
    filter_type = request.args.get("filter_type")
    filter_value = request.args.get("filter_value")

    selected_day = request.args.get("day")
    selected_month = request.args.get("month")

    if filter_type == "day":
        selected_day = filter_value or selected_day
        selected_month = None
    elif filter_type == "month":
        selected_month = filter_value or selected_month
        selected_day = None

    if selected_day:
        filter_type = "day"
    elif selected_month:
        filter_type = "month"
    else:
        filter_type = "month"

    conn = get_db_connection()
    query, params = get_income_filters(user_id, selected_day, selected_month)
    incomes = conn.execute(query, params).fetchall()

    months = conn.execute(
        "SELECT DISTINCT substr(income_date, 1, 7) AS month FROM incomes WHERE user_id = ? ORDER BY month DESC",
        (user_id,),
    ).fetchall()
    days = conn.execute(
        "SELECT DISTINCT income_date AS date FROM incomes WHERE user_id = ? ORDER BY income_date DESC",
        (user_id,),
    ).fetchall()

    providers = conn.execute(
        """
        SELECT
            p.id,
            p.name,
            p.balance,
            COALESCE((
                SELECT pp.amount_paid
                FROM provider_payments pp
                WHERE pp.provider_id = p.id
                ORDER BY pp.payment_date DESC, pp.id DESC
                LIMIT 1
            ), 0.0) AS last_payment_amount,
            p.created_at,
            p.last_balance_date
        FROM providers p
        WHERE p.user_id = ?
        ORDER BY p.created_at DESC
        """,
        (user_id,),
    ).fetchall()

    total_bruto = sum(float(row["amount_bruto"]) for row in incomes)
    total_neto = sum(float(row["amount_neto"]) for row in incomes)
    total_impuestos = sum(float(row["tax_amount"]) for row in incomes)
    total_balance_proveedores = sum(float(row["balance"]) for row in providers)
    total_ultimo_pago_proveedores = sum(float(row["last_payment_amount"]) for row in providers)

    conn.close()

    return render_template(
        "dashboard.html",
        incomes=incomes,
        providers=providers,
        months=months,
        days=days,
        selected_day=selected_day,
        selected_month=selected_month,
        filter_type=filter_type,
        total_bruto=round(total_bruto, 2),
        total_neto=round(total_neto, 2),
        total_impuestos=round(total_impuestos, 2),
        total_balance_proveedores=round(total_balance_proveedores, 2),
        total_ultimo_pago_proveedores=round(total_ultimo_pago_proveedores, 2),
        username=session["username"],
    )


@app.route("/ingresos/nuevo", methods=["GET", "POST"])
@login_required
def add_income():
    if request.method == "POST":
        try:
            amount_bruto = float(request.form["amount_bruto"])
            tax_rate = float(request.form.get("tax_rate", 0) or 0)
        except (ValueError, TypeError):
            flash("Los valores numéricos ingresados no son válidos.")
            return render_template("add_income.html", today=datetime.now().strftime("%Y-%m-%d"))

        if amount_bruto <= 0:
            flash("El ingreso bruto debe ser mayor a 0.")
            return render_template("add_income.html", today=datetime.now().strftime("%Y-%m-%d"))

        if tax_rate < 0 or tax_rate > 100:
            flash("El porcentaje de impuesto debe estar entre 0 y 100.")
            return render_template("add_income.html", today=datetime.now().strftime("%Y-%m-%d"))

        income_date = request.form.get("income_date") or datetime.now().strftime("%Y-%m-%d")

        tax_amount = amount_bruto * (tax_rate / 100)
        amount_neto = amount_bruto - tax_amount

        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO incomes (user_id, amount_bruto, tax_rate, tax_amount, amount_neto, income_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session["user_id"],
                amount_bruto,
                tax_rate,
                tax_amount,
                amount_neto,
                income_date,
            ),
        )
        conn.commit()
        conn.close()

        flash("Ingreso registrado correctamente.")
        return redirect(url_for("dashboard"))

    return render_template("add_income.html", today=datetime.now().strftime("%Y-%m-%d"))


@app.route("/proveedores", methods=["GET", "POST"])
@login_required
def providers_page():
    if request.method == "POST":
        name = normalize_provider_name(request.form.get("name", ""))
        try:
            balance = float(request.form.get("balance", 0) or 0)
        except (ValueError, TypeError):
            flash("El saldo debe ser un número válido.")
            return redirect(url_for("providers_page"))

        if not name:
            flash("El nombre del proveedor es obligatorio.")
            return redirect(url_for("providers_page"))

        if balance <= 0:
            flash("El saldo a ingresar debe ser mayor a 0.")
            return redirect(url_for("providers_page"))

        balance_date = request.form.get("balance_date") or datetime.now().strftime("%Y-%m-%d")

        conn = get_db_connection()
        existing_provider = conn.execute(
            "SELECT * FROM providers WHERE user_id = ? AND LOWER(name) = LOWER(?) ORDER BY created_at DESC LIMIT 1",
            (session["user_id"], name),
        ).fetchone()

        if existing_provider:
            new_balance = float(existing_provider["balance"]) + balance
            conn.execute(
                "UPDATE providers SET name = ?, balance = ?, remaining_balance = ?, last_balance_date = ? WHERE id = ?",
                (name, new_balance, new_balance, balance_date, existing_provider["id"]),
            )
            conn.execute(
                "INSERT INTO provider_movements (provider_id, movement_type, amount, movement_date) VALUES (?, ?, ?, ?)",
                (existing_provider["id"], "balance", balance, balance_date),
            )
            flash("Saldo agregado al proveedor correctamente.")
        else:
            conn.execute(
                "INSERT INTO providers (user_id, name, balance, amount_paid, remaining_balance, last_balance_date) VALUES (?, ?, ?, ?, ?, ?)",
                (session["user_id"], name, balance, 0, balance, balance_date),
            )
            provider_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO provider_movements (provider_id, movement_type, amount, movement_date) VALUES (?, ?, ?, ?)",
                (provider_id, "balance", balance, balance_date),
            )
            flash("Proveedor registrado correctamente.")

        conn.commit()
        conn.close()
        return redirect(url_for("providers_page"))

    filter_type = request.args.get("filter_type")
    filter_value = request.args.get("filter_value")

    selected_day = request.args.get("day")
    selected_month = request.args.get("month")

    if filter_type == "day":
        selected_day = filter_value or selected_day
        selected_month = None
    elif filter_type == "month":
        selected_month = filter_value or selected_month
        selected_day = None

    if selected_day:
        filter_type = "day"
    elif selected_month:
        filter_type = "month"
    else:
        filter_type = "month"

    conn = get_db_connection()
    providers = conn.execute(
        """
        SELECT
            p.id,
            p.name,
            p.balance,
            COALESCE((
                SELECT pp.amount_paid
                FROM provider_payments pp
                WHERE pp.provider_id = p.id
                ORDER BY pp.payment_date DESC, pp.id DESC
                LIMIT 1
            ), 0.0) AS last_payment_amount,
            p.created_at,
            p.last_balance_date
        FROM providers p
        WHERE p.user_id = ?
        ORDER BY p.created_at DESC
        """,
        (session["user_id"],),
    ).fetchall()
    history = get_provider_history(session["user_id"], selected_day, selected_month)
    conn.close()

    return render_template(
        "providers.html",
        providers=providers,
        history=history,
        filter_type=filter_type,
        selected_day=selected_day,
        selected_month=selected_month,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/ingresos/<int:income_id>/eliminar", methods=["POST"])
@login_required
def delete_income(income_id):
    conn = get_db_connection()
    income = conn.execute(
        "SELECT id FROM incomes WHERE id = ? AND user_id = ?",
        (income_id, session["user_id"]),
    ).fetchone()

    if not income:
        conn.close()
        flash("Ingreso no encontrado.")
        return redirect(url_for("dashboard"))

    conn.execute("DELETE FROM incomes WHERE id = ? AND user_id = ?", (income_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Ingreso eliminado correctamente.")
    return redirect(url_for("dashboard"))


@app.route("/proveedores/<int:provider_id>/pago", methods=["POST"])
@login_required
def add_provider_payment(provider_id):
    try:
        amount_paid = float(request.form.get("amount_paid", 0) or 0)
    except (ValueError, TypeError):
        flash("El monto del pago debe ser un número válido.")
        return redirect(url_for("providers_page"))

    payment_date = request.form.get("payment_date") or datetime.now().strftime("%Y-%m-%d")

    if amount_paid <= 0:
        flash("El monto del pago debe ser mayor a cero.")
        return redirect(url_for("providers_page"))

    conn = get_db_connection()
    provider = conn.execute(
        "SELECT * FROM providers WHERE id = ? AND user_id = ?",
        (provider_id, session["user_id"]),
    ).fetchone()

    if not provider:
        conn.close()
        flash("Proveedor no encontrado.")
        return redirect(url_for("providers_page"))

    current_balance = float(provider["balance"])
    if amount_paid > current_balance:
        flash(f"El monto del pago ($ {amount_paid:.2f}) no puede ser mayor al saldo adeudado ($ {current_balance:.2f}).")
        conn.close()
        return redirect(url_for("providers_page"))

    new_balance = round(current_balance - amount_paid, 2)
    new_amount_paid = round(float(provider["amount_paid"]) + amount_paid, 2)

    conn.execute(
        "INSERT INTO provider_payments (provider_id, amount_paid, payment_date) VALUES (?, ?, ?)",
        (provider_id, amount_paid, payment_date),
    )
    conn.execute(
        "UPDATE providers SET balance = ?, amount_paid = ?, remaining_balance = ? WHERE id = ?",
        (new_balance, new_amount_paid, new_balance, provider_id),
    )
    conn.commit()
    conn.close()
    flash("Pago registrado correctamente.")
    return redirect(url_for("providers_page"))


@app.route("/proveedores/<int:provider_id>/eliminar", methods=["POST"])
@login_required
def delete_provider(provider_id):
    conn = get_db_connection()
    provider = conn.execute(
        "SELECT id FROM providers WHERE id = ? AND user_id = ?",
        (provider_id, session["user_id"]),
    ).fetchone()

    if not provider:
        conn.close()
        flash("Proveedor no encontrado.")
        return redirect(url_for("providers_page"))

    # Eliminar archivos físicos de facturas asociadas a este proveedor
    invoices = conn.execute(
        "SELECT filename FROM provider_invoices WHERE provider_id = ? AND user_id = ?",
        (provider_id, session["user_id"]),
    ).fetchall()
    for inv in invoices:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], inv["filename"])
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass

    conn.execute("DELETE FROM provider_invoices WHERE provider_id = ? AND user_id = ?", (provider_id, session["user_id"]))
    conn.execute("DELETE FROM provider_payments WHERE provider_id = ?", (provider_id,))
    conn.execute("DELETE FROM provider_movements WHERE provider_id = ?", (provider_id,))
    conn.execute("DELETE FROM providers WHERE id = ? AND user_id = ?", (provider_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Proveedor eliminado correctamente.")
    return redirect(url_for("providers_page"))


@app.route("/facturas", methods=["GET"])
@login_required
def invoices_index():
    user_id = session["user_id"]
    conn = get_db_connection()
    providers = conn.execute(
        """
        SELECT
            p.id,
            p.name,
            p.balance,
            COUNT(pi.id) AS invoices_count,
            MAX(pi.invoice_date) AS last_invoice_date
        FROM providers p
        LEFT JOIN provider_invoices pi ON pi.provider_id = p.id
        WHERE p.user_id = ?
        GROUP BY p.id, p.name, p.balance
        ORDER BY p.name ASC
        """,
        (user_id,),
    ).fetchall()

    recent_invoices = conn.execute(
        """
        SELECT
            pi.id,
            pi.provider_id,
            p.name AS provider_name,
            pi.invoice_date,
            pi.filename,
            pi.original_filename,
            pi.notes,
            pi.created_at
        FROM provider_invoices pi
        JOIN providers p ON p.id = pi.provider_id
        WHERE pi.user_id = ?
        ORDER BY pi.invoice_date DESC, pi.created_at DESC
        LIMIT 10
        """,
        (user_id,),
    ).fetchall()
    conn.close()

    return render_template(
        "invoices_index.html",
        providers=providers,
        recent_invoices=recent_invoices,
    )


@app.route("/proveedores/<int:provider_id>/facturas", methods=["GET", "POST"])
@login_required
def provider_invoices(provider_id):
    user_id = session["user_id"]
    conn = get_db_connection()
    provider = conn.execute(
        "SELECT * FROM providers WHERE id = ? AND user_id = ?",
        (provider_id, user_id),
    ).fetchone()

    if not provider:
        conn.close()
        flash("Proveedor no encontrado.")
        return redirect(url_for("invoices_index"))

    if request.method == "POST":
        invoice_date = request.form.get("invoice_date") or datetime.now().strftime("%Y-%m-%d")
        notes = request.form.get("notes", "").strip()

        file = request.files.get("invoice_file")
        if not file or file.filename == "":
            conn.close()
            flash("Debes seleccionar un archivo de factura (foto o PDF).")
            return redirect(url_for("provider_invoices", provider_id=provider_id))

        if not allowed_file(file.filename):
            conn.close()
            flash("Formato de archivo no permitido. Solo se aceptan PDF, PNG, JPG, JPEG o WEBP.")
            return redirect(url_for("provider_invoices", provider_id=provider_id))

        original_filename = secure_filename(file.filename) or "factura"
        ext = original_filename.rsplit(".", 1)[1].lower() if "." in original_filename else "pdf"
        unique_filename = f"prov_{provider_id}_{uuid.uuid4().hex[:12]}.{ext}"
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_filename)
        file.save(save_path)

        conn.execute(
            """
            INSERT INTO provider_invoices (user_id, provider_id, invoice_number, amount, invoice_date, filename, original_filename, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                provider_id,
                "",
                0.0,
                invoice_date,
                unique_filename,
                original_filename,
                notes,
            ),
        )
        conn.commit()
        conn.close()

        flash("Factura subida correctamente.")
        return redirect(url_for("provider_invoices", provider_id=provider_id))

    # Filtros para el historial de facturas
    filter_type = request.args.get("filter_type")
    filter_value = request.args.get("filter_value")
    selected_day = request.args.get("day")
    selected_month = request.args.get("month")

    if filter_type == "day":
        selected_day = filter_value or selected_day
        selected_month = None
    elif filter_type == "month":
        selected_month = filter_value or selected_month
        selected_day = None

    if selected_day:
        filter_type = "day"
    elif selected_month:
        filter_type = "month"
    else:
        filter_type = "month"

    query = """
        SELECT *
        FROM provider_invoices
        WHERE user_id = ? AND provider_id = ?
    """
    params = [user_id, provider_id]

    if selected_day:
        query += " AND invoice_date = ?"
        params.append(selected_day)
    elif selected_month:
        query += " AND substr(invoice_date, 1, 7) = ?"
        params.append(selected_month)

    query += " ORDER BY invoice_date DESC, created_at DESC"
    invoices = conn.execute(query, params).fetchall()
    total_invoices_count = len(invoices)
    conn.close()

    return render_template(
        "provider_invoices.html",
        provider=provider,
        invoices=invoices,
        total_invoices_count=total_invoices_count,
        filter_type=filter_type,
        selected_day=selected_day,
        selected_month=selected_month,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/facturas/<int:invoice_id>/archivo", methods=["GET"])
@login_required
def view_invoice_file(invoice_id):
    user_id = session["user_id"]
    conn = get_db_connection()
    invoice = conn.execute(
        "SELECT * FROM provider_invoices WHERE id = ? AND user_id = ?",
        (invoice_id, user_id),
    ).fetchone()
    conn.close()

    if not invoice:
        flash("Factura no encontrada.")
        return redirect(url_for("invoices_index"))

    return send_from_directory(
        app.config["UPLOAD_FOLDER"],
        invoice["filename"],
        as_attachment=False,
    )


@app.route("/facturas/<int:invoice_id>/eliminar", methods=["POST"])
@login_required
def delete_invoice(invoice_id):
    user_id = session["user_id"]
    conn = get_db_connection()
    invoice = conn.execute(
        "SELECT * FROM provider_invoices WHERE id = ? AND user_id = ?",
        (invoice_id, user_id),
    ).fetchone()

    if not invoice:
        conn.close()
        flash("Factura no encontrada.")
        return redirect(url_for("invoices_index"))

    provider_id = invoice["provider_id"]
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], invoice["filename"])
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass

    conn.execute("DELETE FROM provider_invoices WHERE id = ? AND user_id = ?", (invoice_id, user_id))
    conn.commit()
    conn.close()
    flash("Factura eliminada correctamente.")
    return redirect(url_for("provider_invoices", provider_id=provider_id))


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
