
import hashlib
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from files.ansible import app, socketio
from files.auth import (
    _check_csrf,
    _safe_next,
    get_db,
    db_one,
    db_run,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

USERNAME_RE = re.compile(r"^[^@]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OTP_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
RESET_MINUTES = 30
PASSWORD_MIN = 3
PASSWORD_MAX = 12

SESSION_JS_VERSION = 1


def init_user_tables():
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_otps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                otp_hash TEXT NOT NULL,
                username TEXT,
                user_id INTEGER,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


init_user_tables()


def _now():
    return datetime.now(timezone.utc)


def _validate_password(password, confirm):
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return f"Password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters."

    if password != confirm:
        return "Passwords do not match."

    return None


def _create_user_session(user):
    session.clear()
    session.permanent = True
    session["session_id"] = secrets.token_urlsafe(32)
    session["user_id"] = int(user["id"])
    session["user"] = user["username"]
    session["_csrf"] = secrets.token_hex(16)
    session["login_time"] = _now().isoformat()


def _send_otp_email(to_email, otp, purpose):
    mail_user = os.environ.get("MAIL_USER", "").strip()
    mail_password = os.environ.get("MAIL_PASSWORD", "").strip()
    mail_host = os.environ.get("MAIL_HOST", "smtp.gmail.com").strip()
    mail_port = int(os.environ.get("MAIL_PORT", "587"))
    mail_from = os.environ.get("MAIL_FROM", mail_user).strip()

    if not mail_user or not mail_password or not mail_from:
        raise RuntimeError("SMTP credentials are not configured.")

    subjects = {
        "register": "Verify your Learning Labs account",
        "login": "Your Learning Labs login verification code",
        "reset": "Your Learning Labs password reset code",
    }

    msg = EmailMessage()
    msg["Subject"] = subjects[purpose]
    msg["From"] = mail_from
    msg["To"] = to_email

    msg.set_content(
        f"Your Learning Labs verification code is: {otp}\n\n"
        f"This code expires in {OTP_MINUTES} minutes.\n"
        f"Do not share this code with anyone.\n\n"
        f"If you did not request this code, ignore this email."
    )

    if mail_port == 465:
        with smtplib.SMTP_SSL(mail_host, mail_port, timeout=20) as server:
            server.login(mail_user, mail_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(mail_host, mail_port, timeout=20) as server:
            server.starttls()
            server.login(mail_user, mail_password)
            server.send_message(msg)


def _issue_otp(email, purpose, username=None, user_id=None):
    otp = f"{secrets.randbelow(1000000):06d}"
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()
    now = _now()

    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE email_otps
            SET used = 1
            WHERE email = ? AND purpose = ? AND used = 0
            """,
            (email, purpose),
        )

        cur = conn.execute(
            """
            INSERT INTO email_otps
            (email, purpose, otp_hash, username, user_id,
             expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email,
                purpose,
                otp_hash,
                username,
                user_id,
                (now + timedelta(minutes=OTP_MINUTES)).isoformat(),
                now.isoformat(),
            ),
        )

        otp_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    try:
        _send_otp_email(email, otp, purpose)
    except Exception:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE email_otps SET used = 1 WHERE id = ?",
                (otp_id,),
            )
            conn.commit()
        finally:
            conn.close()
        raise

    session["otp_id"] = otp_id
    session["otp_purpose"] = purpose
    session["otp_email"] = email
    session["otp_next"] = session.get("otp_next", "/")
    return otp_id


def _get_valid_otp():
    otp_id = session.get("otp_id")

    if not otp_id:
        return None

    row = db_one(
        "SELECT * FROM email_otps WHERE id = ? AND used = 0",
        (otp_id,),
    )

    if not row:
        return None

    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        return None

    if datetime.fromisoformat(row["expires_at"]) < _now():
        return None

    return row


def _clear_otp_session():
    for key in (
        "otp_id",
        "otp_purpose",
        "otp_email",
        "otp_next",
    ):
        session.pop(key, None)


def _mark_otp_used(otp_id):
    db_run(
        "UPDATE email_otps SET used = 1 WHERE id = ?",
        (otp_id,),
    )


def _find_valid_reset(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()

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

    if datetime.fromisoformat(row["expires_at"]) < _now():
        return None

    return row


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/new-account")
def new_account():
    return redirect(url_for("register"))


@app.get("/api/whoami")
def whoami():
    return {
        "success": bool(session.get("user_id")),
        "user": session.get("user"),
        "session_id": session.get("session_id"),
    }


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        _check_csrf()

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()

        if not USERNAME_RE.match(username):
            flash("Username must be 3-30 characters and cannot contain @.", "error")
            return render_template("register.html")

        if not EMAIL_RE.match(email):
            flash("Enter a valid email address.", "error")
            return render_template("register.html")

        if db_one(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (username, email),
        ):
            flash("Username or email is already registered.", "error")
            return render_template("register.html")

        try:
            _issue_otp(email, "register", username=username)
            session["otp_next"] = "/"
            flash("Verification code sent to your email.", "success")
            return redirect(url_for("verify_otp"))
        except Exception as exc:
            app.logger.error("Registration OTP email failed: %s", exc)
            flash("Unable to send verification email. Please try again.", "error")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next(request.values.get("next", ""))

    if request.method == "POST":
        _check_csrf()

        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")

        user = db_one(
            """
            SELECT *
            FROM users
            WHERE username = ? OR email = ?
            """,
            (identifier, identifier.lower()),
        )

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username/email or password.", "error")
            return render_template("login.html", next_url=next_url)

        try:
            _issue_otp(user["email"], "login", user_id=user["id"])
            session["otp_next"] = next_url
            flash("Verification code sent to your email.", "success")
            return redirect(url_for("verify_otp"))
        except Exception as exc:
            app.logger.error("Login OTP email failed: %s", exc)
            flash("Unable to send verification email. Please try again.", "error")

    return render_template("login.html", next_url=next_url)


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    purpose = session.get("otp_purpose")

    if purpose not in ("register", "login", "reset"):
        return redirect(url_for("login"))

    if request.method == "POST":
        _check_csrf()

        row = _get_valid_otp()

        if not row:
            _clear_otp_session()
            flash("Verification code expired. Please start again.", "error")
            return redirect(url_for("login"))

        otp = request.form.get("otp", "").strip()
        otp_hash = hashlib.sha256(otp.encode()).hexdigest()

        if not secrets.compare_digest(otp_hash, row["otp_hash"]):
            db_run(
                "UPDATE email_otps SET attempts = attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            flash("Invalid verification code.", "error")
            return render_template("verify_otp.html", purpose=purpose)

        if purpose in ("register", "reset"):
            password = request.form.get("password", "")
            confirm = request.form.get("confirm", "")
            error = _validate_password(password, confirm)

            if error:
                flash(error, "error")
                return render_template("verify_otp.html", purpose=purpose)

        if purpose == "register":
            try:
                db_run(
                    """
                    INSERT INTO users
                    (username, email, password_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        row["username"],
                        row["email"],
                        generate_password_hash(password),
                        _now().isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                flash("Username or email is already registered.", "error")
                _mark_otp_used(row["id"])
                _clear_otp_session()
                return redirect(url_for("register"))

            user = db_one(
                "SELECT * FROM users WHERE email = ?",
                (row["email"],),
            )
            _mark_otp_used(row["id"])
            _clear_otp_session()
            _create_user_session(user)
            flash("Account verified and created successfully.", "success")
            return redirect("/")

        if purpose == "login":
            user = db_one(
                "SELECT * FROM users WHERE id = ?",
                (row["user_id"],),
            )
            _mark_otp_used(row["id"])
            next_url = session.get("otp_next", "/")
            _clear_otp_session()

            if not user:
                flash("Account not found.", "error")
                return redirect(url_for("login"))

            _create_user_session(user)
            return redirect(_safe_next(next_url))

        if purpose == "reset":
            db_run(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(password), row["user_id"]),
            )
            _mark_otp_used(row["id"])
            db_run(
                "UPDATE password_resets SET used = 1 WHERE user_id = ?",
                (row["user_id"],),
            )
            _clear_otp_session()
            flash("Password updated. Please log in.", "success")
            return redirect(url_for("login"))

    return render_template("verify_otp.html", purpose=purpose)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        _check_csrf()

        email = request.form.get("email", "").strip().lower()
        user = db_one("SELECT * FROM users WHERE email = ?", (email,))

        if user:
            try:
                _issue_otp(email, "reset", user_id=user["id"])
                flash("Verification code sent to your email.", "success")
                return redirect(url_for("verify_otp"))
            except Exception as exc:
                app.logger.error("Password reset OTP email failed: %s", exc)

        flash(
            "If that email is registered, a verification code has been sent.",
            "success",
        )
        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html")


# Retain support for previously issued password-reset links.
@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset = _find_valid_reset(token)

    if not reset:
        flash("This reset link is invalid or has expired.", "error")
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
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(password), reset["user_id"]),
            )
            db_run(
                "UPDATE password_resets SET used = 1 WHERE user_id = ?",
                (reset["user_id"],),
            )
            flash("Password updated. You can log in now.", "success")
            return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


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
