
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor

from dotenv import dotenv_values
from flask import flash, redirect, render_template, request, session, url_for

from files.ansible import app
from files.auth import _check_csrf, db_run, get_db
from files.config import execute_script, convert_placeholders

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
LABS = {"ansible", "jenkins", "bash"}
_FALLBACK_PW = secrets.token_urlsafe(10)


def _admin_creds():
    cfg = dotenv_values(ENV_PATH, encoding="utf-8-sig")
    user = (cfg.get("ADMIN_USER") or os.environ.get("ADMIN_USER") or "admin").strip()
    pw = (cfg.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD") or "").strip()
    return user, pw or _FALLBACK_PW


_u, _p = _admin_creds()
print(f"\n[Admin] .env: {ENV_PATH}", flush=True)
print(f"[Admin] user={_u} password_length={len(_p)} from_env_file={_p != _FALLBACK_PW}", flush=True)
if _p == _FALLBACK_PW:
    print(f"[Admin] .env not read. Temporary login -> {_u} / {_p}\n", flush=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_tracking():
    conn = get_db()
    try:
        execute_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS logins (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lab_visits (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                lab TEXT NOT NULL,
                at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount NUMERIC(12, 2) NOT NULL,
                utr TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            """
        )
    finally:
        conn.close()


init_tracking()


@app.template_filter("ist")
def ist(value):
    if not value:
        return "—"
    dt = datetime.fromisoformat(value) + timedelta(hours=5, minutes=30)
    return dt.strftime("%d %b %Y, %I:%M %p")


@app.before_request
def track_activity():
    if session.get("is_admin"):
        return

    uid = session.get("user_id")
    if not uid:
        return

    if not session.get("_login_logged"):
        db_run("INSERT INTO logins (user_id, at) VALUES (?, ?)", (uid, _now()))
        session["_login_logged"] = True

    if request.method == "GET":
        first = request.path.strip("/").split("/")[0]
        if first in LABS:
            seen = session.get("_labs_seen", [])
            if first not in seen:
                db_run(
                    "INSERT INTO lab_visits (user_id, lab, at) VALUES (?, ?, ?)",
                    (uid, first, _now()),
                )
                session["_labs_seen"] = seen + [first]


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        _check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user, pw = _admin_creds()

        if secrets.compare_digest(username.encode(), user.encode()) and secrets.compare_digest(
            password.encode(), pw.encode()
        ):
            session.clear()
            session.permanent = True
            session["is_admin"] = True

            # Reserved identity for admin lab access.
            # Normal registered users receive positive database IDs.
            session["user_id"] = -1
            session["user"] = "admin"
            session["session_id"] = secrets.token_urlsafe(32)
            session["_csrf"] = secrets.token_hex(16)
            session["login_time"] = datetime.now(timezone.utc).isoformat()

            return redirect(url_for("admin_dashboard"))

        print(
            f"[Admin] Login failed: typed user='{username}' (expected '{user}'), "
            f"typed password length={len(password)} (expected {len(pw)})",
            flush=True,
        )
        flash("Invalid admin credentials.", "error")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


USERS_SQL = """
SELECT u.id, u.username, u.email, u.created_at,
  (SELECT MAX(at) FROM logins WHERE user_id = u.id) AS last_login,
  (SELECT COUNT(*) FROM logins WHERE user_id = u.id) AS login_count,
  (SELECT STRING_AGG(DISTINCT lab, ',') FROM lab_visits WHERE user_id = u.id) AS labs,
  (SELECT lab FROM lab_visits WHERE user_id = u.id ORDER BY at DESC LIMIT 1) AS current_lab,
  (SELECT COALESCE(SUM(amount), 0) FROM payments WHERE user_id = u.id AND status = 'verified') AS paid
FROM users u
ORDER BY (SELECT MAX(at) FROM logins WHERE user_id = u.id) DESC NULLS LAST
"""

ACTIVITY_SQL = """
SELECT u.username AS username, 'login' AS kind, '' AS detail, l.at AS at
  FROM logins l JOIN users u ON u.id = l.user_id
UNION ALL
SELECT u.username, 'lab', v.lab, v.at
  FROM lab_visits v JOIN users u ON u.id = v.user_id
ORDER BY at DESC LIMIT 50
"""


@app.route("/admin")
@admin_required
def admin_dashboard():
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn = get_db()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(USERS_SQL)
            users = cur.fetchall()

            cur.execute(
                "SELECT p.*, u.username, u.email FROM payments p JOIN users u ON u.id = p.user_id "
                "WHERE p.status = 'pending' ORDER BY p.created_at DESC"
            )
            pending = cur.fetchall()

            cur.execute(
                "SELECT p.*, u.username FROM payments p JOIN users u ON u.id = p.user_id "
                "WHERE p.status != 'pending' ORDER BY p.created_at DESC LIMIT 100"
            )
            history = cur.fetchall()

            cur.execute(ACTIVITY_SQL)
            activity = cur.fetchall()

            cur.execute(
                "SELECT v.lab AS lab, u.username AS username, COUNT(*) AS visits, MAX(v.at) AS last_at "
                "FROM lab_visits v JOIN users u ON u.id = v.user_id "
                "GROUP BY v.lab, u.id ORDER BY v.lab, last_at DESC"
            )
            by_lab = cur.fetchall()

            cur.execute(
                convert_placeholders(
                    "SELECT (SELECT COUNT(*) FROM users) AS users, "
                    "(SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'verified') AS payers, "
                    "(SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'verified') AS total, "
                    "(SELECT COUNT(DISTINCT user_id) FROM logins WHERE at >= ?) AS active"
                ),
                (day_ago,),
            )
            stats = cur.fetchone()

    finally:
        conn.close()

    return render_template(
        "admin.html",
        users=users,
        pending=pending,
        history=history,
        activity=activity,
        by_lab=by_lab,
        stats=stats,
    )


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    _check_csrf()

    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username FROM users WHERE id = %s",
                (user_id,),
            )
            user = cur.fetchone()

            if not user:
                flash("User not found.", "error")
                return redirect(url_for("admin_dashboard"))

            # Delete associated records before deleting the user.
            for table in ("payments", "lab_visits", "logins", "password_resets"):
                cur.execute(
                    f"DELETE FROM {table} WHERE user_id = %s",
                    (user_id,),
                )

            cur.execute(
                "DELETE FROM users WHERE id = %s",
                (user_id,),
            )

        conn.commit()
        flash(f"User {user[1]} deleted successfully.", "success")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/payment/<int:pid>", methods=["POST"])
@admin_required
def admin_payment(pid):
    _check_csrf()
    action = request.form.get("action")

    if action in ("verified", "rejected"):
        db_run("UPDATE payments SET status = ? WHERE id = ?", (action, pid))

    return redirect(url_for("admin_dashboard"))


@app.route("/contribute/submit", methods=["POST"])
def contribute_submit():
    if not session.get("user_id") or session.get("is_admin"):
        return redirect(url_for("login", next="/contribute"))

    _check_csrf()
    utr = re.sub(r"\s", "", request.form.get("utr", ""))

    try:
        amount = Decimal(request.form.get("amount", ""))
    except InvalidOperation:
        amount = Decimal(0)

    if not re.fullmatch(r"\d{12}", utr):
        flash("Enter the 12-digit UPI reference (UTR) number.", "error")

    elif amount < 1 or amount > 100000:
        flash("Enter a valid amount between 1 and 100000.", "error")

    else:
        try:
            db_run(
                "INSERT INTO payments (user_id, amount, utr, created_at) VALUES (?, ?, ?, ?)",
                (session["user_id"], float(amount), utr, _now()),
            )
            flash("Thank you! Your payment is submitted and awaiting verification.", "success")

        except psycopg2.IntegrityError:
            flash("This reference number was already submitted.", "error")

    return redirect(url_for("contribute"))

