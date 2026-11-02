
import hashlib
import os
import re
import secrets
import smtplib
import psycopg2
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import join_room
from werkzeug.security import check_password_hash, generate_password_hash

from files.ansible import app, socketio, terminal_lock, terminal_sessions

from files.auth import (
    _check_csrf,
    _safe_next,
    get_db,
    db_one,
    db_run,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "users.db")
KEY_PATH = os.path.join(BASE_DIR, ".secret_key")

USERNAME_RE = re.compile(r"^[^@]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

RESET_MINUTES = 30
PASSWORD_MIN = 3
PASSWORD_MAX = 12

SESSION_JS_VERSION = 1


# ---------- secret key ----------

def _load_secret_key():
    env_key = os.environ.get("SECRET_KEY")

    if env_key:
        return env_key

    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as f:
            return f.read().strip()

    key = secrets.token_hex(32)

    with open(KEY_PATH, "w") as f:
        f.write(key)

    return key


if not app.secret_key:
    app.secret_key = _load_secret_key()

app.config.setdefault("SESSION_COOKIE_NAME", "learning_labs_session")
app.config.setdefault("SESSION_COOKIE_PATH", "/")
app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
app.config.setdefault("SESSION_COOKIE_SECURE", False)
app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(days=7))

socketio.manage_session = True


# ---------- database ----------

def init_db():
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS password_resets (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                token_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


init_db()


# ---------- helpers ----------

def _csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)

    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = _csrf_token


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(
                url_for("login", next=request.path)
            )

        return view(*args, **kwargs)

    return wrapped


def _validate_password(password, confirm):
    if not (
        PASSWORD_MIN <= len(password) <= PASSWORD_MAX
    ):
        return (
            f"Password must be "
            f"{PASSWORD_MIN}-{PASSWORD_MAX} characters."
        )

    if password != confirm:
        return "Passwords do not match."

    return None


def _close_user_terminals(uid):
    with terminal_lock:
        keys = [
            key
            for key, value in terminal_sessions.items()
            if value.get("uid") == uid
        ]

        closing = [
            terminal_sessions.pop(key)
            for key in keys
        ]

    for term in closing:
        for part in ("channel", "client"):
            try:
                term[part].close()
            except Exception:
                pass


def _create_user_session(user):
    session.clear()

    session.permanent = True
    session["session_id"] = secrets.token_urlsafe(32)
    session["user_id"] = int(user["id"])
    session["user"] = user["username"]
    session["_csrf"] = secrets.token_hex(16)
    session["login_time"] = datetime.now(
        timezone.utc
    ).isoformat()


def _send_reset_email(to_email, link):
    host = os.environ.get("MAIL_HOST")

    if not host:
        print(
            f"\n[Password reset] Link for {to_email}: {link}\n",
            flush=True,
        )
        return

    try:
        msg = EmailMessage()
        msg["Subject"] = "Reset your Learning Labs password"
        msg["From"] = os.environ.get(
            "MAIL_FROM",
            os.environ.get("MAIL_USER", "no-reply@learninglabs"),
        )
        msg["To"] = to_email

        msg.set_content(
            f"Use the link below to reset your password. "
            f"It expires in {RESET_MINUTES} minutes.\n\n"
            f"{link}\n\n"
            f"If you did not request this, "
            f"you can ignore this email."
        )

        with smtplib.SMTP(
            host,
            int(os.environ.get("MAIL_PORT", "587")),
        ) as server:
            server.starttls()

            mail_user = os.environ.get("MAIL_USER")

            if mail_user:
                server.login(
                    mail_user,
                    os.environ.get("MAIL_PASSWORD", ""),
                )

            server.send_message(msg)

    except Exception as exc:
        print(
            f"[Password reset] Email failed ({exc}). "
            f"Link: {link}",
            flush=True,
        )


def _find_valid_reset(token):
    token_hash = hashlib.sha256(
        token.encode()
    ).hexdigest()

    row = db_one(
        """
        SELECT *
        FROM password_resets
        WHERE token_hash = ? AND used = 0
        """,
        (token_hash,),
    )

    if not row:
        return None

    if (
        datetime.fromisoformat(row["expires_at"])
        < datetime.now(timezone.utc)
    ):
        return None

    return row


# ---------- routes ----------

@app.route("/new-account")
def new_account():
    return redirect(url_for("login"))


@app.get("/api/whoami")
def whoami():
    return jsonify({
        "success": bool(session.get("user_id")),
        "user": session.get("user"),
        "session_id": session.get("session_id"),
    })


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "")

    if request.method == "POST":
        _check_csrf()

        identifier = request.form.get(
            "identifier", ""
        ).strip()

        password = request.form.get("password", "")

        user = db_one(
            """
            SELECT *
            FROM users
            WHERE username = ? OR email = ?
            """,
            (identifier, identifier.lower()),
        )

        if (
            user
            and check_password_hash(
                user["password_hash"],
                password,
            )
        ):
            _create_user_session(user)
            return redirect(_safe_next(next_url))

        flash(
            "Invalid username/email or password.",
            "error",
        )

    return render_template(
        "login.html",
        next_url=next_url,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        _check_csrf()

        username = request.form.get(
            "username", ""
        ).strip()

        email = request.form.get(
            "email", ""
        ).strip().lower()

        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        error = None

        if not USERNAME_RE.match(username):
            error = (
                "Username must be 3-30 characters "
                "and cannot contain @."
            )
        elif not EMAIL_RE.match(email):
            error = "Enter a valid email address."
        else:
            error = _validate_password(password, confirm)

        if error:
            flash(error, "error")
        else:
            try:
                db_run(
                    """
                    INSERT INTO users
                    (username, email, password_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(password),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

                flash(
                    "Account created. You can log in now.",
                    "success",
                )

                return redirect(url_for("login"))

            except psycopg2.IntegrityError:
                flash(
                    "Username or email is already registered.",
                    "error",
                )

    return render_template("register.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        _check_csrf()

        email = request.form.get(
            "email", ""
        ).strip().lower()

        user = db_one(
            "SELECT * FROM users WHERE email = ?",
            (email,),
        )

        if user:
            token = secrets.token_urlsafe(32)

            db_run(
                """
                INSERT INTO password_resets
                (user_id, token_hash, expires_at)
                VALUES (?, ?, ?)
                """,
                (
                    user["id"],
                    hashlib.sha256(token.encode()).hexdigest(),
                    (
                        datetime.now(timezone.utc)
                        + timedelta(minutes=RESET_MINUTES)
                    ).isoformat(),
                ),
            )

            _send_reset_email(
                user["email"],
                url_for(
                    "reset_password",
                    token=token,
                    _external=True,
                ),
            )

        flash(
            "If that email is registered, "
            "a reset link has been sent.",
            "success",
        )

        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html")


@app.route(
    "/reset-password/<token>",
    methods=["GET", "POST"],
)
def reset_password(token):
    reset = _find_valid_reset(token)

    if not reset:
        flash(
            "This reset link is invalid or has expired.",
            "error",
        )
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        _check_csrf()

        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        error = _validate_password(password, confirm)

        if error:
            flash(error, "error")
        else:
            db_run(
                """
                UPDATE users
                SET password_hash = ?
                WHERE id = ?
                """,
                (
                    generate_password_hash(password),
                    reset["user_id"],
                ),
            )

            db_run(
                """
                UPDATE password_resets
                SET used = 1
                WHERE user_id = ?
                """,
                (reset["user_id"],),
            )

            flash(
                "Password updated. You can log in now.",
                "success",
            )

            return redirect(url_for("login"))

    return render_template(
        "reset_password.html",
        token=token,
    )


@app.route("/logout")
def logout():
    session_id = session.get("session_id")
    session.clear()

    if session_id:
        socketio.emit(
            "logged_out",
            {"session_id": session_id},
            to="session-" + session_id,
        )

    return redirect(url_for("login"))


# Admin@12345
