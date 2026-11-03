
import os
import hashlib
import re
import secrets
import smtplib
import time

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps

from flask import (
    request,
    render_template,
    redirect,
    url_for,
    flash,
    session,
    jsonify,
)
from werkzeug.security import generate_password_hash, check_password_hash
from psycopg2 import IntegrityError

from files.ansible import app, socketio
from files.auth import (
    _check_csrf,
    _safe_next,
    get_db,
    db_one,
    db_run,
)


USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

OTP_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60

RESET_MINUTES = 30
PASSWORD_MIN = 3
PASSWORD_MAX = 12


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


def _mail_setting(name, default=None):
    return app.config.get(name) or os.getenv(name) or default


def _parse_datetime(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def init_user_tables():
    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS email_otps (
                    id BIGSERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    otp_hash TEXT NOT NULL,
                    username TEXT,
                    user_id BIGINT,
                    expires_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    used INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_email_otps_email
                ON email_otps (email)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_email_otps_lookup
                ON email_otps (email, purpose, used)
            """)

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def _send_otp_email(email, otp):
    host = _mail_setting("MAIL_HOST")
    port = int(_mail_setting("MAIL_PORT", "587"))
    username = _mail_setting("MAIL_USER")
    password = _mail_setting("MAIL_PASSWORD")
    sender = _mail_setting("MAIL_FROM", username)

    if not all([host, username, password, sender]):
        raise RuntimeError("SMTP configuration is missing.")

    message = EmailMessage()
    message["Subject"] = "Your Learning Labs verification code"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"""Your verification code is: {otp}

This code expires in {OTP_MINUTES} minutes.

If you did not request this code, ignore this email.
"""
    )

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(username, password)
            server.send_message(message)


def _issue_otp(email, purpose, username=None, user_id=None):
    otp = f"{secrets.randbelow(1000000):06d}"
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()

    expires_at = (_now() + timedelta(minutes=OTP_MINUTES)).isoformat()
    created_at = _now_iso()

    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE email_otps
                SET used = 1
                WHERE email = %s
                  AND purpose = %s
                  AND used = 0
                """,
                (email, purpose),
            )

            cur.execute(
                """
                INSERT INTO email_otps
                    (email, purpose, otp_hash, username, user_id,
                     expires_at, attempts, used, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s)
                RETURNING id
                """,
                (
                    email,
                    purpose,
                    otp_hash,
                    username,
                    user_id,
                    expires_at,
                    created_at,
                ),
            )

            otp_id = cur.fetchone()[0]

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    try:
        _send_otp_email(email, otp)
    except Exception:
        db_run(
            "UPDATE email_otps SET used = 1 WHERE id = ?",
            (otp_id,),
        )
        raise

    session["otp_id"] = otp_id
    session["otp_email"] = email
    session["otp_purpose"] = purpose
    session["otp_username"] = username
    session["otp_user_id"] = user_id
    session["otp_last_sent"] = time.time()

    return otp_id


def _get_valid_otp():
    otp_id = session.get("otp_id")

    if not otp_id:
        return None

    return db_one(
        "SELECT * FROM email_otps WHERE id = ?",
        (otp_id,),
    )


def _clear_otp_session():
    for key in (
        "otp_id",
        "otp_email",
        "otp_purpose",
        "otp_username",
        "otp_user_id",
        "otp_last_sent",
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
        WHERE token_hash = ?
          AND used = 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (token_hash,),
    )

    if not row:
        return None

    expires_at = _parse_datetime(row.get("expires_at"))

    if not expires_at or expires_at <= _now():
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
    return render_template("register.html")


@app.route("/api/whoami")
def whoami():
    if not session.get("user_id"):
        return jsonify({"authenticated": False})

    return jsonify({
        "authenticated": True,
        "user_id": session.get("user_id"),
        "username": session.get("username"),
        "email": session.get("email"),
    })


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    _check_csrf()

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()

    if not USERNAME_RE.fullmatch(username):
        flash("Username must be 3-30 characters.", "error")
        return redirect(url_for("new_account"))

    if not EMAIL_RE.fullmatch(email):
        flash("Enter a valid email address.", "error")
        return redirect(url_for("new_account"))

    existing_user = db_one(
        "SELECT id FROM users WHERE username = ? OR email = ? LIMIT 1",
        (username, email),
    )

    if existing_user:
        flash("Username or email already exists.", "error")
        return redirect(url_for("new_account"))

    try:
        _issue_otp(email, "register", username=username)
        session["otp_next"] = url_for("login")

        flash("Verification code sent to your email.", "success")
        return redirect(url_for("verify_otp"))

    except Exception:
        app.logger.exception("Registration OTP delivery failed")
        flash("Unable to send verification code. Please try again.", "error")
        return redirect(url_for("new_account"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user_id"):
            return redirect(_safe_next(request.args.get("next")))

        return render_template("login.html")

    _check_csrf()

    identity = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    next_url = _safe_next(request.form.get("next"))

    user = db_one(
        """
        SELECT *
        FROM users
        WHERE username = ? OR email = ?
        LIMIT 1
        """,
        (identity, identity.lower()),
    )

    if not user or not check_password_hash(user["password_hash"], password):
        flash("Invalid username/email or password.", "error")
        return redirect(url_for("login", next=next_url))

    try:
        _issue_otp(
            user["email"],
            "login",
            username=user["username"],
            user_id=user["id"],
        )

        session["otp_next"] = next_url

        flash("Verification code sent to your registered email.", "success")
        return redirect(url_for("verify_otp"))

    except Exception:
        app.logger.exception("Login OTP delivery failed")
        flash("Unable to send verification code. Please try again.", "error")
        return redirect(url_for("login", next=next_url))


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if not session.get("otp_id"):
        flash("No active verification request.", "error")
        return redirect(url_for("login"))

    if request.method == "GET":
        last_sent = session.get("otp_last_sent", 0)
        remaining = max(
            0,
            OTP_RESEND_SECONDS - int(time.time() - last_sent),
        )

        return render_template(
            "verify_otp.html",
            email=session.get("otp_email"),
            purpose=session.get("otp_purpose"),
            resend_wait=remaining,
        )

    _check_csrf()

    otp = request.form.get("otp", "").strip()

    if not re.fullmatch(r"\d{6}", otp):
        flash("Enter a valid six-digit verification code.", "error")
        return redirect(url_for("verify_otp"))

    row = _get_valid_otp()

    if not row or row["used"]:
        flash("Verification code is invalid or expired. Resend a new code.", "error")
        return redirect(url_for("verify_otp"))

    expires_at = _parse_datetime(row["expires_at"])

    if not expires_at or expires_at <= _now():
        flash("Verification code has expired. Resend a new code.", "error")
        return redirect(url_for("verify_otp"))

    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        _mark_otp_used(row["id"])
        flash("Maximum attempts reached. Resend a new code.", "error")
        return redirect(url_for("verify_otp"))

    submitted_hash = hashlib.sha256(otp.encode()).hexdigest()

    if not secrets.compare_digest(submitted_hash, row["otp_hash"]):
        db_run(
            """
            UPDATE email_otps
            SET attempts = attempts + 1
            WHERE id = ?
            """,
            (row["id"],),
        )

        flash("Invalid verification code. Please try again.", "error")
        return redirect(url_for("verify_otp"))

    if row["purpose"] == "register":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm", "")

        if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            flash(
                f"Password must be between {PASSWORD_MIN} and {PASSWORD_MAX} characters.",
                "error",
            )
            return redirect(url_for("verify_otp"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("verify_otp"))

        try:
            db_run(
                """
                INSERT INTO users (username, email, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    row["username"],
                    row["email"],
                    generate_password_hash(password),
                    _now_iso(),
                ),
            )

        except IntegrityError:
            flash("Username or email already exists.", "error")
            _mark_otp_used(row["id"])
            _clear_otp_session()
            return redirect(url_for("new_account"))

        _mark_otp_used(row["id"])
        _clear_otp_session()

        flash("Account created successfully. Please log in.", "success")
        return redirect(url_for("login"))

    if row["purpose"] == "login":
        user = db_one(
            "SELECT * FROM users WHERE id = ? LIMIT 1",
            (row["user_id"],),
        )

        if not user:
            _mark_otp_used(row["id"])
            _clear_otp_session()
            flash("User account not found.", "error")
            return redirect(url_for("login"))

        _mark_otp_used(row["id"])
        next_url = session.get("otp_next", "/")

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["email"] = user["email"]

        return redirect(_safe_next(next_url))

    _mark_otp_used(row["id"])
    _clear_otp_session()

    flash("Invalid verification request.", "error")
    return redirect(url_for("login"))


@app.route("/resend-otp", methods=["POST"])
def resend_otp():
    _check_csrf()

    email = session.get("otp_email")
    purpose = session.get("otp_purpose")

    if not email or not purpose:
        flash("No active verification request.", "error")
        return redirect(url_for("login"))

    last_sent = session.get("otp_last_sent", 0)
    elapsed = time.time() - last_sent

    if elapsed < OTP_RESEND_SECONDS:
        remaining = OTP_RESEND_SECONDS - int(elapsed)
        flash(
            f"Please wait {remaining} seconds before requesting another code.",
            "error",
        )
        return redirect(url_for("verify_otp"))

    username = session.get("otp_username")
    user_id = session.get("otp_user_id")
    next_url = session.get("otp_next", "/")

    try:
        _issue_otp(
            email,
            purpose,
            username=username,
            user_id=user_id,
        )

        session["otp_next"] = next_url
        flash("A new verification code has been sent.", "success")

    except Exception:
        app.logger.exception("Resending OTP failed")
        flash("Unable to resend verification code. Please try again.", "error")

    return redirect(url_for("verify_otp"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    _check_csrf()

    email = request.form.get("email", "").strip().lower()

    if not EMAIL_RE.fullmatch(email):
        flash("Enter a valid email address.", "error")
        return redirect(url_for("forgot_password"))

    user = db_one(
        "SELECT id, email FROM users WHERE email = ? LIMIT 1",
        (email,),
    )

    if user:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires_at = (_now() + timedelta(minutes=RESET_MINUTES)).isoformat()

        db_run(
            """
            UPDATE password_resets
            SET used = 1
            WHERE user_id = ?
            """,
            (user["id"],),
        )

        db_run(
            """
            INSERT INTO password_resets
                (user_id, token_hash, expires_at, used)
            VALUES (?, ?, ?, 0)
            """,
            (user["id"], token_hash, expires_at),
        )

        reset_url = url_for(
            "reset_password",
            token=token,
            _external=True,
        )

        try:
            host = _mail_setting("MAIL_HOST")
            port = int(_mail_setting("MAIL_PORT", "587"))
            mail_user = _mail_setting("MAIL_USER")
            mail_password = _mail_setting("MAIL_PASSWORD")
            sender = _mail_setting("MAIL_FROM", mail_user)

            message = EmailMessage()
            message["Subject"] = "Learning Labs password reset"
            message["From"] = sender
            message["To"] = email
            message.set_content(
                f"Use this link to reset your password:\n\n{reset_url}\n\n"
                f"This link expires in {RESET_MINUTES} minutes."
            )

            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                    server.login(mail_user, mail_password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=20) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(mail_user, mail_password)
                    server.send_message(message)

        except Exception:
            app.logger.exception("Password reset email failed")

    flash(
        "If the account exists, a password reset link has been sent.",
        "success",
    )
    return redirect(url_for("login"))


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset = _find_valid_reset(token)

    if not reset:
        flash("This password reset link is invalid or expired.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "GET":
        return render_template("reset_password.html", token=token)

    _check_csrf()

    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        flash(
            f"Password must be between {PASSWORD_MIN} and {PASSWORD_MAX} characters.",
            "error",
        )
        return redirect(url_for("reset_password", token=token))

    if password != confirm_password:
        flash("Passwords do not match.", "error")
        return redirect(url_for("reset_password", token=token))

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
        WHERE id = ?
        """,
        (reset["id"],),
    )

    flash("Password reset successfully. Please log in.", "success")
    return redirect(url_for("login"))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "POST":
        _check_csrf()

    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


init_user_tables()
