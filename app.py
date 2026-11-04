import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import psycopg
from flask import (
    Flask,
    abort,
    current_app,
    g,
    jsonify,
    redirect,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash


PROJECT_ROOT = Path(__file__).resolve().parent
PROTECTED_PAGES = {
    "astro-essentials.html",
    "birth-details.html",
    "exclusive-yantra.html",
    "kundli-report.html",
    "premium-gemstone.html",
    "rashi-yantra.html",
    "standard-gemstone.html",
}
STATIC_FILES = {
    "astro.mp4",
    "astrologer-video.mp4",
    "jyotish-logo.jpeg",
    "talk2jyotisha-logo.svg",
    "site-auth.js",
}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 128


class User(UserMixin):
    def __init__(self, user_id, email, full_name):
        self.id = str(user_id)
        self.email = email
        self.full_name = full_name


class _PostgresRow(dict):
    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _postgres_row_factory(cursor):
    names = [column.name for column in cursor.description]
    return lambda values: _PostgresRow(names, values)


def _postgres_sql(query):
    query = query.replace("BEGIN IMMEDIATE", "BEGIN")
    query = query.replace("COLLATE NOCASE", "")
    if query.lstrip().upper().startswith("CREATE TABLE"):
        query = re.sub(
            r"\bINTEGER PRIMARY KEY(?!\s+REFERENCES)",
            "BIGSERIAL PRIMARY KEY",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(
            r"\bINTEGER(\s+PRIMARY KEY)?\s+REFERENCES\b",
            lambda match: "BIGINT" + (match.group(1) or "") + " REFERENCES",
            query,
            flags=re.IGNORECASE,
        )
    return query.replace("?", "%s")


class _PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    @property
    def in_transaction(self):
        return self._connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    def execute(self, query, parameters=()):
        return self._connection.execute(_postgres_sql(query), parameters)

    def executemany(self, query, parameters):
        return self._connection.cursor().executemany(
            _postgres_sql(query),
            parameters,
        )

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def close(self):
        self._connection.close()


def _development_secret(private_dir):
    private_dir.mkdir(parents=True, exist_ok=True)
    secret_path = private_dir / "secret.key"
    try:
        with secret_path.open("x", encoding="ascii") as secret_file:
            secret_file.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    if os.name != "nt":
        os.chmod(secret_path, 0o600)
    return secret_path.read_text(encoding="ascii").strip()


def _database_connection():
    if "database" not in g:
        if current_app.config["DATABASE_URL"]:
            if not current_app.config["DATABASE_INITIALIZED"]:
                with current_app.config["DATABASE_INIT_LOCK"]:
                    if not current_app.config["DATABASE_INITIALIZED"]:
                        _initialize_database(current_app._get_current_object())
                        current_app.config["DATABASE_INITIALIZED"] = True
            g.database = _PostgresConnection(
                psycopg.connect(
                    current_app.config["DATABASE_URL"],
                    autocommit=True,
                    row_factory=_postgres_row_factory,
                )
            )
        else:
            g.database = sqlite3.connect(
                current_app.config["DATABASE"],
                timeout=10,
                isolation_level=None,
            )
            g.database.row_factory = sqlite3.Row
            g.database.execute("PRAGMA foreign_keys = ON")
            g.database.execute("PRAGMA busy_timeout = 10000")
    return g.database


def _initialize_database(app):
    if app.config["DATABASE_URL"]:
        connection = _PostgresConnection(
            psycopg.connect(
                app.config["DATABASE_URL"],
                autocommit=True,
                row_factory=_postgres_row_factory,
            )
        )
        try:
            connection.execute("BEGIN")
            connection.execute("SELECT pg_advisory_xact_lock(72819463021)")
            _initialize_database_schema(connection)
            connection.execute(
                """
                INSERT INTO wallet_accounts (user_id, balance_cents, updated_at)
                SELECT id, 0, ? FROM users
                ON CONFLICT (user_id) DO NOTHING
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )
            connection.execute("COMMIT")
        except psycopg.Error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return

    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    _initialize_database_schema(connection)
    connection.execute(
        """
        INSERT OR IGNORE INTO wallet_accounts (user_id, balance_cents, updated_at)
        SELECT id, 0, ? FROM users
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    connection.close()
    if os.name != "nt":
        os.chmod(database_path, 0o600)


def _initialize_database_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS birth_details (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            full_name TEXT NOT NULL,
            date_of_birth TEXT NOT NULL,
            time_of_birth TEXT,
            birth_time_unknown INTEGER NOT NULL,
            birth_time_accuracy TEXT NOT NULL,
            place_of_birth TEXT NOT NULL,
            country TEXT NOT NULL,
            region TEXT NOT NULL,
            gender TEXT NOT NULL,
            preferred_language TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            attempt_key TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS login_attempts_key_time
            ON login_attempts(attempt_key, attempted_at);
        CREATE TABLE IF NOT EXISTS signup_attempts (
            attempt_key TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS signup_attempts_key_time
            ON signup_attempts(attempt_key, attempted_at);
        CREATE TABLE IF NOT EXISTS kundli_requests (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            birth_details_snapshot TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS birthplace_cache (
            location_key TEXT PRIMARY KEY,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            timezone TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS birth_charts (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            profile_hash TEXT NOT NULL,
            birth_profile_snapshot TEXT NOT NULL,
            chart_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            UNIQUE(user_id, profile_hash)
        );
        CREATE INDEX IF NOT EXISTS birth_charts_user_generated
            ON birth_charts(user_id, generated_at DESC);
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            chart_id INTEGER REFERENCES birth_charts(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL CHECK (
                category IN ('kundli', 'puja', 'consultation', 'gemstone', 'other')
            ),
            status TEXT NOT NULL,
            file_key TEXT NOT NULL UNIQUE,
            media_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS documents_user_category_date
            ON documents(user_id, category, created_at DESC);
        CREATE TABLE IF NOT EXISTS wallet_accounts (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            transaction_id TEXT NOT NULL UNIQUE,
            transaction_type TEXT NOT NULL CHECK (
                transaction_type IN ('credit', 'debit', 'refund')
            ),
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'completed', 'failed', 'refunded')
            ),
            payment_reference TEXT UNIQUE,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS wallet_transactions_user_date
            ON wallet_transactions(user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS puja_records (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            puja_name TEXT NOT NULL,
            description TEXT NOT NULL,
            booked_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL,
            receipt_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS puja_records_user_date
            ON puja_records(user_id, created_at DESC);
        """
    )


def _json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _normalized_text(value, label, maximum, required=True):
    if not isinstance(value, str):
        value = ""
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > maximum:
        raise ValueError(f"{label} must be {maximum} characters or fewer.")
    return value


def _user_for_id(user_id):
    row = _database_connection().execute(
        "SELECT id, email, full_name FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return User(row["id"], row["email"], row["full_name"])


def _attempt_keys(email):
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    remote_address = request.remote_addr or "unknown"
    values = (f"email:{email}|ip:{remote_address}", f"ip:{remote_address}")
    return [
        hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
        for value in values
    ]


def _signup_attempt_key():
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    remote_address = request.remote_addr or "unknown"
    return hmac.new(
        secret,
        f"signup-ip:{remote_address}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _is_signup_rate_limited(connection, attempt_key):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    count = connection.execute(
        "SELECT COUNT(*) FROM signup_attempts WHERE attempt_key = ? AND attempted_at > ?",
        (attempt_key, cutoff),
    ).fetchone()[0]
    return count >= 10


def _record_signup_attempt(connection, attempt_key):
    now = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    connection.execute(
        "INSERT INTO signup_attempts (attempt_key, attempted_at) VALUES (?, ?)",
        (attempt_key, now),
    )
    connection.execute("DELETE FROM signup_attempts WHERE attempted_at < ?", (cutoff,))


def _is_login_rate_limited(connection, attempt_keys):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    placeholders = ",".join("?" for _ in attempt_keys)
    rows = connection.execute(
        f"""
        SELECT attempt_key, COUNT(*) AS attempts
        FROM login_attempts
        WHERE attempt_key IN ({placeholders}) AND attempted_at > ?
        GROUP BY attempt_key
        """,
        (*attempt_keys, cutoff),
    ).fetchall()
    counts = {row["attempt_key"]: row["attempts"] for row in rows}
    return counts.get(attempt_keys[0], 0) >= 5 or counts.get(attempt_keys[1], 0) >= 20


def _record_failed_login(connection, attempt_keys):
    now = datetime.now(timezone.utc).isoformat()
    connection.executemany(
        "INSERT INTO login_attempts (attempt_key, attempted_at) VALUES (?, ?)",
        ((key, now) for key in attempt_keys),
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    connection.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))


def _details_from_row(row):
    if row is None:
        return None
    return {
        "fullName": row["full_name"],
        "dateOfBirth": row["date_of_birth"],
        "timeOfBirth": row["time_of_birth"] or "",
        "birthTimeUnknown": bool(row["birth_time_unknown"]),
        "birthTimeAccuracy": row["birth_time_accuracy"],
        "placeOfBirth": row["place_of_birth"],
        "country": row["country"],
        "region": row["region"],
        "gender": row["gender"],
        "preferredLanguage": row["preferred_language"],
        "updatedAt": row["updated_at"],
    }


def _validated_birth_details(data):
    full_name = _normalized_text(data.get("fullName"), "Full name", 120)
    place = _normalized_text(data.get("placeOfBirth"), "City or town of birth", 120)
    country = _normalized_text(data.get("country"), "Country", 100)
    region = _normalized_text(data.get("region"), "State", 100)
    gender = _normalized_text(data.get("gender"), "Gender", 40, required=False)
    preferred_language = _normalized_text(
        data.get("preferredLanguage"),
        "Preferred language",
        60,
        required=False,
    )

    birth_date = data.get("dateOfBirth")
    if not isinstance(birth_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth_date):
        raise ValueError("Date of birth is required.")
    try:
        parsed_date = date.fromisoformat(birth_date)
    except ValueError as error:
        raise ValueError("Enter a valid date of birth.") from error
    if parsed_date > date.today():
        raise ValueError("Date of birth cannot be in the future.")

    birth_time_unknown = data.get("birthTimeUnknown") is True
    time_value = data.get("timeOfBirth", "")
    if birth_time_unknown:
        time_value = None
        accuracy = ""
    else:
        if not isinstance(time_value, str) or not time_value:
            raise ValueError("Time of birth is required unless you do not know it.")
        try:
            time.fromisoformat(time_value)
        except ValueError as error:
            raise ValueError("Enter a valid time of birth.") from error
        if len(time_value) != 5:
            raise ValueError("Enter the birth time in HH:MM format.")
        accuracy = data.get("birthTimeAccuracy", "exact")
        if not isinstance(accuracy, str) or accuracy not in {"exact", "approximate"}:
            raise ValueError("Choose exact or approximate birth-time accuracy.")

    if gender and gender not in {"female", "male", "non-binary", "other"}:
        raise ValueError("Choose a valid gender option.")
    if len(preferred_language) > 60:
        raise ValueError("Preferred language must be 60 characters or fewer.")

    return (
        full_name,
        parsed_date.isoformat(),
        time_value,
        int(birth_time_unknown),
        accuracy,
        place,
        country,
        region,
        gender,
        preferred_language,
    )


def _birthplace_location(connection, details, user_agent):
    location_query = "|".join(
        (details["placeOfBirth"], details["region"], details["country"])
    ).casefold()
    location_key = hashlib.sha256(location_query.encode("utf-8")).hexdigest()
    row = connection.execute(
        """
        SELECT latitude, longitude, timezone
        FROM birthplace_cache
        WHERE location_key = ?
        """,
        (location_key,),
    ).fetchone()
    if row is not None:
        return {
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "timezone": row["timezone"],
        }

    from birth_chart import geocode_birthplace

    location = geocode_birthplace(details, user_agent)
    connection.execute(
        """
        INSERT INTO birthplace_cache (
            location_key, latitude, longitude, timezone, created_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (location_key) DO NOTHING
        """,
        (
            location_key,
            location["latitude"],
            location["longitude"],
            location["timezone"],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    row = connection.execute(
        """
        SELECT latitude, longitude, timezone
        FROM birthplace_cache
        WHERE location_key = ?
        """,
        (location_key,),
    ).fetchone()
    return {
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row["timezone"],
    }


def _chart_payload(row):
    if row is None:
        return None
    chart = json.loads(row["chart_json"])
    chart["chartId"] = row["id"]
    chart["generatedAt"] = row["generated_at"]
    return chart


def _document_payload(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "chartId": row["chart_id"],
        "downloadUrl": url_for("download_document", document_id=row["id"]),
    }


def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    default_private_dir = (
        Path("/tmp") / "t2j-private"
        if os.environ.get("VERCEL")
        else PROJECT_ROOT / ".t2j-private"
    )
    private_dir = Path(os.environ.get("T2J_PRIVATE_DIR", default_private_dir))
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(private_dir, 0o700)
    production = (
        os.environ.get("T2J_ENV", "").lower() == "production"
        or os.environ.get("VERCEL_ENV", "").lower() == "production"
    )
    secret_key = os.environ.get("T2J_SECRET_KEY")
    if production and not secret_key:
        raise RuntimeError("Set T2J_SECRET_KEY to a strong random value in production.")
    if not secret_key:
        secret_key = _development_secret(private_dir)

    app.config.from_mapping(
        SECRET_KEY=secret_key,
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        DATABASE_INITIALIZED=False,
        DATABASE_INIT_LOCK=threading.Lock(),
        DATABASE=os.environ.get(
            "T2J_DATABASE",
            str(private_dir / "talk2jyotisha.sqlite3"),
        ),
        VERCEL_DEPLOYMENT=bool(os.environ.get("VERCEL")),
        DOCUMENTS_DIRECTORY=str(private_dir / "documents"),
        GEOCODER_USER_AGENT=os.environ.get("T2J_GEOCODER_USER_AGENT", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production
        or os.environ.get("T2J_COOKIE_SECURE", "").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        WTF_CSRF_TIME_LIMIT=7200,
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DOCUMENTS_DIRECTORY"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    if not app.config["DATABASE_URL"]:
        _initialize_database(app)
        app.config["DATABASE_INITIALIZED"] = True

    CSRFProtect(app)
    login_manager = LoginManager(app)

    @app.teardown_appcontext
    def close_database(_error=None):
        database = g.pop("database", None)
        if database is not None:
            database.close()

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return _user_for_id(int(user_id))
        except (TypeError, ValueError):
            return None

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify(error="Please log in to continue."), 401
        return redirect(url_for("home", next=request.path))

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error="This request could not be verified. Reload the page and try again."), 400
        return error.description, 400

    @app.errorhandler(psycopg.Error)
    def database_error(error):
        app.logger.exception("Persistent PostgreSQL storage is unavailable.")
        if request.path.startswith("/api/"):
            return jsonify(
                error="Persistent account storage is unavailable. Check the Vercel DATABASE_URL setting and redeploy."
            ), 503
        return "Persistent account storage is temporarily unavailable.", 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error

    @app.before_request
    def require_authenticated_pages():
        page_name = request.path.lstrip("/")
        if page_name in PROTECTED_PAGES and not current_user.is_authenticated:
            return redirect(url_for("home", next=request.path))
        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    @app.get("/index.html")
    def home():
        return send_from_directory(PROJECT_ROOT, "index.html")

    @app.get("/api/csrf")
    def csrf_token():
        return jsonify(csrfToken=generate_csrf())

    @app.get("/api/me")
    def current_account():
        user = None
        if current_user.is_authenticated:
            user = {"email": current_user.email, "fullName": current_user.full_name}
        return jsonify(user=user, csrfToken=generate_csrf())

    @app.post("/api/signup")
    def signup():
        if current_app.config["VERCEL_DEPLOYMENT"] and not current_app.config["DATABASE_URL"]:
            return jsonify(
                error="Account storage is not configured. Set DATABASE_URL to a persistent PostgreSQL database."
            ), 503

        connection = _database_connection()
        attempt_key = _signup_attempt_key()
        if _is_signup_rate_limited(connection, attempt_key):
            return jsonify(error="Too many account creation attempts. Please try again later."), 429
        _record_signup_attempt(connection, attempt_key)

        data = _json_object()
        try:
            full_name = _normalized_text(data.get("fullName"), "Full name", 120)
            email = _normalized_text(data.get("email"), "Email address", 254).lower()
            password = data.get("password")
            if not EMAIL_PATTERN.fullmatch(email):
                raise ValueError("Enter a valid email address.")
            if not isinstance(password, str) or not (
                PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH
            ):
                raise ValueError(
                    f"Password must be between {PASSWORD_MIN_LENGTH} and {PASSWORD_MAX_LENGTH} characters."
                )
        except ValueError as error:
            return jsonify(error=str(error)), 400

        try:
            cursor = connection.execute(
                """
                INSERT INTO users (email, full_name, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                RETURNING id
                """,
                (
                    email,
                    full_name,
                    generate_password_hash(password, method="scrypt"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except (sqlite3.IntegrityError, psycopg.IntegrityError):
            return jsonify(error="An account with this email already exists. Please log in."), 409

        user = User(cursor.fetchone()["id"], email, full_name)
        connection.execute(
            """
            INSERT INTO wallet_accounts (user_id, balance_cents, updated_at)
            VALUES (?, 0, ?)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user.id, datetime.now(timezone.utc).isoformat()),
        )
        session.clear()
        login_user(user, fresh=True)
        session.permanent = True
        return jsonify(
            user={"email": user.email, "fullName": user.full_name},
            csrfToken=generate_csrf(),
        ), 201

    @app.post("/api/login")
    def login():
        if current_app.config["VERCEL_DEPLOYMENT"] and not current_app.config["DATABASE_URL"]:
            return jsonify(
                error="Account storage is not configured. Set DATABASE_URL to a persistent PostgreSQL database."
            ), 503

        data = _json_object()
        email = data.get("email", "")
        password = data.get("password", "")
        if not isinstance(email, str) or not isinstance(password, str):
            return jsonify(error="Enter a valid email address and password."), 400
        email = email.strip().lower()
        if (
            not EMAIL_PATTERN.fullmatch(email)
            or not password
            or len(password) > PASSWORD_MAX_LENGTH
        ):
            return jsonify(error="Enter a valid email address and password."), 400

        connection = _database_connection()
        attempt_keys = _attempt_keys(email)
        if _is_login_rate_limited(connection, attempt_keys):
            return jsonify(error="Too many sign-in attempts. Please wait 15 minutes and try again."), 429

        user_row = connection.execute(
            "SELECT id, email, full_name, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        password_hash = (
            user_row["password_hash"]
            if user_row is not None
            else current_app.config["DUMMY_PASSWORD_HASH"]
        )
        password_matches = check_password_hash(password_hash, password)
        if user_row is None or not password_matches:
            _record_failed_login(connection, attempt_keys)
            return jsonify(error="Invalid email or password."), 401

        connection.execute(
            "DELETE FROM login_attempts WHERE attempt_key IN (?, ?)",
            attempt_keys,
        )
        user = User(user_row["id"], user_row["email"], user_row["full_name"])
        session.clear()
        login_user(user, fresh=True)
        session.permanent = True
        return jsonify(
            user={"email": user.email, "fullName": user.full_name},
            csrfToken=generate_csrf(),
        )

    @app.post("/api/logout")
    @login_required
    def logout():
        logout_user()
        session.clear()
        return jsonify(csrfToken=generate_csrf())

    @app.get("/api/profile")
    @login_required
    def get_profile():
        row = _database_connection().execute(
            "SELECT * FROM birth_details WHERE user_id = ?",
            (current_user.id,),
        ).fetchone()
        return jsonify(details=_details_from_row(row))

    @app.put("/api/profile")
    @login_required
    def update_profile():
        data = _json_object()
        try:
            details = _validated_birth_details(data)
        except ValueError as error:
            return jsonify(error=str(error)), 400

        connection = _database_connection()
        updated_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO birth_details (
                user_id, full_name, date_of_birth, time_of_birth,
                birth_time_unknown, birth_time_accuracy, place_of_birth,
                country, region, gender, preferred_language, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name,
                date_of_birth = excluded.date_of_birth,
                time_of_birth = excluded.time_of_birth,
                birth_time_unknown = excluded.birth_time_unknown,
                birth_time_accuracy = excluded.birth_time_accuracy,
                place_of_birth = excluded.place_of_birth,
                country = excluded.country,
                region = excluded.region,
                gender = excluded.gender,
                preferred_language = excluded.preferred_language,
                updated_at = excluded.updated_at
            """,
            (current_user.id, *details, updated_at),
        )
        connection.execute(
            "UPDATE users SET full_name = ? WHERE id = ?",
            (details[0], current_user.id),
        )
        current_user.full_name = details[0]
        row = connection.execute(
            "SELECT * FROM birth_details WHERE user_id = ?",
            (current_user.id,),
        ).fetchone()
        return jsonify(details=_details_from_row(row))

    @app.get("/api/dashboard")
    @login_required
    def dashboard_summary():
        from birth_chart import name_based_rashi

        connection = _database_connection()
        profile_row = connection.execute(
            "SELECT * FROM birth_details WHERE user_id = ?",
            (current_user.id,),
        ).fetchone()
        chart_row = connection.execute(
            """
            SELECT chart_json, generated_at, id
            FROM birth_charts
            WHERE user_id = ?
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (current_user.id,),
        ).fetchone()
        document_count = connection.execute(
            "SELECT COUNT(*) FROM documents WHERE user_id = ?",
            (current_user.id,),
        ).fetchone()[0]
        completed_pujas = connection.execute(
            """
            SELECT COUNT(*) FROM puja_records
            WHERE user_id = ? AND status = 'completed'
            """,
            (current_user.id,),
        ).fetchone()[0]
        wallet = connection.execute(
            "SELECT balance_cents FROM wallet_accounts WHERE user_id = ?",
            (current_user.id,),
        ).fetchone()
        chart = _chart_payload(chart_row)
        details = _details_from_row(profile_row)
        display_name = details["fullName"] if details else current_user.full_name
        return jsonify(
            overview={
                "profileComplete": details is not None,
                "moonSign": chart["moonSign"] if chart else None,
                "nameRashi": name_based_rashi(display_name),
                "ascendant": chart["ascendant"]["sign"] if chart else None,
                "nakshatra": chart["nakshatra"]["name"] if chart else None,
                "chartGenerated": chart is not None,
                "documentCount": document_count,
                "completedPujas": completed_pujas,
                "upcomingConsultations": None,
                "walletBalanceCents": wallet["balance_cents"] if wallet else 0,
            }
        )

    @app.get("/api/charts")
    @login_required
    def list_birth_charts():
        rows = _database_connection().execute(
            """
            SELECT id, chart_json, generated_at
            FROM birth_charts
            WHERE user_id = ?
            ORDER BY generated_at DESC
            LIMIT 50
            """,
            (current_user.id,),
        ).fetchall()
        return jsonify(charts=[_chart_payload(row) for row in rows])

    @app.get("/api/charts/<int:chart_id>")
    @login_required
    def get_birth_chart(chart_id):
        row = _database_connection().execute(
            """
            SELECT id, chart_json, generated_at
            FROM birth_charts
            WHERE id = ? AND user_id = ?
            """,
            (chart_id, current_user.id),
        ).fetchone()
        if row is None:
            abort(404)
        return jsonify(chart=_chart_payload(row))

    @app.post("/api/charts/generate")
    @login_required
    def generate_birth_chart():
        from birth_chart import (
            ChartConfigurationError,
            ChartInputError,
            calculate_birth_chart,
            profile_fingerprint,
            render_chart_pdf,
        )

        connection = _database_connection()
        details_row = connection.execute(
            "SELECT * FROM birth_details WHERE user_id = ?",
            (current_user.id,),
        ).fetchone()
        details = _details_from_row(details_row)
        if details is None:
            return jsonify(error="Add your personal birth details before generating a chart."), 400
        if details["birthTimeUnknown"]:
            return jsonify(error="Add your exact time of birth to calculate an Ascendant and houses."), 400
        if details["birthTimeAccuracy"] != "exact":
            return jsonify(error="Set birth-time accuracy to Exact before generating a chart."), 400

        fingerprint = profile_fingerprint(details)
        existing = connection.execute(
            """
            SELECT id, chart_json, generated_at
            FROM birth_charts
            WHERE user_id = ? AND profile_hash = ?
            """,
            (current_user.id, fingerprint),
        ).fetchone()
        if existing is not None:
            document = connection.execute(
                "SELECT id, name, category, status, created_at, chart_id FROM documents WHERE chart_id = ? AND user_id = ?",
                (existing["id"], current_user.id),
            ).fetchone()
            return jsonify(
                chart=_chart_payload(existing),
                document=_document_payload(document) if document else None,
                reused=True,
            )

        try:
            location = _birthplace_location(
                connection,
                details,
                current_app.config["GEOCODER_USER_AGENT"],
            )
            chart = calculate_birth_chart(details, location)
            pdf_data = render_chart_pdf(details, chart)
        except ChartInputError as error:
            return jsonify(error=str(error)), 400
        except ChartConfigurationError as error:
            return jsonify(error=str(error)), 503

        birth_profile_snapshot = json.dumps(details, sort_keys=True)
        file_key = f"{uuid.uuid4().hex}.pdf"
        file_path = Path(current_app.config["DOCUMENTS_DIRECTORY"]) / file_key
        connection.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            existing = connection.execute(
                """
                SELECT id, chart_json, generated_at
                FROM birth_charts
                WHERE user_id = ? AND profile_hash = ?
                """,
                (current_user.id, fingerprint),
            ).fetchone()
            if existing is not None:
                connection.execute("ROLLBACK")
                document = connection.execute(
                    "SELECT id, name, category, status, created_at, chart_id FROM documents WHERE chart_id = ? AND user_id = ?",
                    (existing["id"], current_user.id),
                ).fetchone()
                return jsonify(
                    chart=_chart_payload(existing),
                    document=_document_payload(document) if document else None,
                    reused=True,
                )

            with file_path.open("xb") as report_file:
                report_file.write(pdf_data)
            generated_at = chart["generatedAt"]
            chart_cursor = connection.execute(
                """
                INSERT INTO birth_charts (
                    user_id, profile_hash, birth_profile_snapshot,
                    chart_json, generated_at
                ) VALUES (?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    current_user.id,
                    fingerprint,
                    birth_profile_snapshot,
                    json.dumps(chart, ensure_ascii=True),
                    generated_at,
                ),
            )
            chart_id = chart_cursor.fetchone()["id"]
            document_cursor = connection.execute(
                """
                INSERT INTO documents (
                    user_id, chart_id, name, category, status,
                    file_key, media_type, created_at
                ) VALUES (?, ?, ?, 'kundli', 'available', ?, 'application/pdf', ?)
                RETURNING id
                """,
                (
                    current_user.id,
                    chart_id,
                    f"Vedic Birth Chart - {details['dateOfBirth']}",
                    file_key,
                    generated_at,
                ),
            )
            chart["chartId"] = chart_id
            document_id = document_cursor.fetchone()["id"]
            chart["documentId"] = document_id
            connection.execute(
                "UPDATE birth_charts SET chart_json = ? WHERE id = ?",
                (json.dumps(chart, ensure_ascii=True), chart_id),
            )
            connection.execute("COMMIT")
            committed = True
        finally:
            if not committed:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if file_path.exists():
                    file_path.unlink()

        document = connection.execute(
            "SELECT id, name, category, status, created_at, chart_id FROM documents WHERE id = ? AND user_id = ?",
            (document_id, current_user.id),
        ).fetchone()
        row = connection.execute(
            "SELECT id, chart_json, generated_at FROM birth_charts WHERE id = ? AND user_id = ?",
            (chart_id, current_user.id),
        ).fetchone()
        return jsonify(
            chart=_chart_payload(row),
            document=_document_payload(document),
            reused=False,
        ), 201

    @app.get("/api/documents")
    @login_required
    def list_documents():
        category = request.args.get("category", "").strip().lower()
        search = request.args.get("q", "").strip()[:100]
        allowed_categories = {"kundli", "puja", "consultation", "gemstone", "other"}
        if category and category not in allowed_categories:
            return jsonify(error="Choose a valid document category."), 400
        conditions = ["user_id = ?"]
        parameters = [current_user.id]
        if category:
            conditions.append("category = ?")
            parameters.append(category)
        if search:
            conditions.append("name LIKE ? ESCAPE '\\'")
            escaped_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{escaped_search}%")
        rows = _database_connection().execute(
            f"""
            SELECT id, name, category, status, created_at, chart_id
            FROM documents
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
            LIMIT 100
            """,
            parameters,
        ).fetchall()
        return jsonify(documents=[_document_payload(row) for row in rows])

    @app.get("/api/documents/<int:document_id>/download")
    @login_required
    def download_document(document_id):
        row = _database_connection().execute(
            """
            SELECT name, file_key, media_type
            FROM documents
            WHERE id = ? AND user_id = ?
            """,
            (document_id, current_user.id),
        ).fetchone()
        if row is None:
            abort(404)
        file_key = row["file_key"]
        if Path(file_key).name != file_key or not file_key.endswith(".pdf"):
            abort(404)
        file_path = Path(current_app.config["DOCUMENTS_DIRECTORY"]) / file_key
        if not file_path.is_file():
            abort(404)
        from werkzeug.utils import secure_filename

        return send_file(
            file_path,
            mimetype=row["media_type"],
            as_attachment=True,
            download_name=secure_filename(f"{row['name']}.pdf"),
            conditional=True,
        )

    @app.get("/api/wallet")
    @login_required
    def get_wallet():
        connection = _database_connection()
        connection.execute(
            """
            INSERT INTO wallet_accounts (user_id, balance_cents, updated_at)
            VALUES (?, 0, ?)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (current_user.id, datetime.now(timezone.utc).isoformat()),
        )
        account = connection.execute(
            "SELECT balance_cents, updated_at FROM wallet_accounts WHERE user_id = ?",
            (current_user.id,),
        ).fetchone()
        totals = connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN transaction_type = 'credit' AND status = 'completed' THEN amount_cents ELSE 0 END), 0) AS total_added,
                COALESCE(SUM(CASE WHEN transaction_type = 'debit' AND status = 'completed' THEN amount_cents ELSE 0 END), 0) AS total_spent
            FROM wallet_transactions
            WHERE user_id = ?
            """,
            (current_user.id,),
        ).fetchone()
        transactions = connection.execute(
            """
            SELECT transaction_id, transaction_type, amount_cents, status, description, created_at
            FROM wallet_transactions
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 25
            """,
            (current_user.id,),
        ).fetchall()
        return jsonify(
            wallet={
                "balanceCents": account["balance_cents"],
                "totalAddedCents": totals["total_added"],
                "totalSpentCents": totals["total_spent"],
                "currency": "USD",
                "paymentConfigured": False,
                "transactions": [
                    {
                        "transactionId": row["transaction_id"],
                        "type": row["transaction_type"],
                        "amountCents": row["amount_cents"],
                        "status": row["status"],
                        "description": row["description"],
                        "createdAt": row["created_at"],
                    }
                    for row in transactions
                ],
            }
        )

    @app.post("/api/wallet/topups")
    @login_required
    def request_wallet_topup():
        return jsonify(
            error="Wallet deposits are unavailable because no verified payment gateway is configured."
        ), 503

    @app.get("/api/pujas")
    @login_required
    def list_puja_records():
        rows = _database_connection().execute(
            """
            SELECT id, puja_name, description, booked_at, completed_at, status
            FROM puja_records
            WHERE user_id = ?
            ORDER BY COALESCE(completed_at, booked_at, created_at) DESC
            LIMIT 100
            """,
            (current_user.id,),
        ).fetchall()
        return jsonify(
            pujas=[
                {
                    "id": row["id"],
                    "name": row["puja_name"],
                    "description": row["description"],
                    "bookedAt": row["booked_at"],
                    "completedAt": row["completed_at"],
                    "status": row["status"],
                }
                for row in rows
            ]
        )

    @app.post("/api/kundli-requests")
    @login_required
    def create_kundli_request():
        data = _json_object()
        try:
            message = _normalized_text(
                data.get("message"),
                "Message",
                1000,
                required=False,
            )
        except ValueError as error:
            return jsonify(error=str(error)), 400

        connection = _database_connection()
        birth_row = connection.execute(
            "SELECT * FROM birth_details WHERE user_id = ?",
            (current_user.id,),
        ).fetchone()
        if birth_row is None:
            return jsonify(error="Add your birth details before requesting a Kundli report."), 400

        request_id = connection.execute(
            """
            INSERT INTO kundli_requests (
                user_id, birth_details_snapshot, message, created_at
            ) VALUES (?, ?, ?, ?)
            RETURNING id
            """,
            (
                current_user.id,
                json.dumps(_details_from_row(birth_row)),
                message,
                datetime.now(timezone.utc).isoformat(),
            ),
        ).fetchone()["id"]
        return jsonify(
            requestId=request_id,
            status="pending",
            message="Your request has been saved. This is a request only; no appointment or report is confirmed.",
        ), 201

    @app.get("/<path:filename>")
    def project_file(filename):
        if filename in PROTECTED_PAGES:
            return send_from_directory(PROJECT_ROOT, filename)
        if filename in STATIC_FILES:
            return send_from_directory(PROJECT_ROOT, filename)
        abort(404)

    app.config["DUMMY_PASSWORD_HASH"] = generate_password_hash(
        secrets.token_urlsafe(24),
        method="scrypt",
    )
    return app


if __name__ == "__main__":
    application = create_app()
    application.run(
        host=os.environ.get("T2J_HOST", "127.0.0.1"),
        port=int(os.environ.get("T2J_PORT", "8765")),
        debug=os.environ.get("T2J_DEBUG", "").lower() == "true",
    )
