import sqlite3
from datetime import date

import pytest

import app as app_module
from app import create_app
from birth_chart import (
    ChartInputError,
    _birth_time_utc,
    _lahiri_ayanamsa,
    name_based_rashi,
)


@pytest.fixture
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-key-not-used-outside-tests",
            "DATABASE": str(tmp_path / "talk2jyotisha.sqlite3"),
            "DOCUMENTS_DIRECTORY": str(tmp_path / "documents"),
            "GEOCODER_USER_AGENT": "Talk2Jyotisha-test/1.0",
            "WTF_CSRF_ENABLED": True,
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client):
    return client.get("/api/csrf").get_json()["csrfToken"]


def signup(client, name, email, password="A-secure-test-password-123"):
    return client.post(
        "/api/signup",
        json={"fullName": name, "email": email, "password": password},
        headers={"X-CSRFToken": csrf(client)},
    )


def valid_birth_details(name="A Test User"):
    return {
        "fullName": name,
        "dateOfBirth": "1990-05-10",
        "timeOfBirth": "12:30",
        "birthTimeUnknown": False,
        "birthTimeAccuracy": "exact",
        "placeOfBirth": "Jaipur",
        "country": "India",
        "region": "Rajasthan",
        "gender": "",
        "preferredLanguage": "Hindi",
    }


def test_homepage_is_public_but_site_and_dashboard_pages_are_protected(client):
    assert client.get("/").status_code == 200
    response = client.get("/index.html")
    assert b'id="navAccountLink"' in response.data
    assert b'id="navSignupLink"' in response.data
    assert b'data-auth-required hidden>Profile</a>' in response.data
    assert b'data-auth-required hidden>My Dashboard</a>' in response.data
    assert b'onclick="return requireAccountForBooking(event)"' in response.data
    assert b"Please sign up or log in before booking an appointment." in client.get(
        "/site-auth.js"
    ).data
    assert b'onclick="openHistoryModal()">My Bookings</a>' not in response.data

    for path in (
        "/birth-details.html",
        "/kundli-report.html",
        "/astro-essentials.html",
        "/exclusive-yantra.html",
        "/premium-gemstone.html",
        "/rashi-yantra.html",
        "/standard-gemstone.html",
    ):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/index.html?next=")


def test_signup_hashes_password_and_rejects_duplicate_account(client, app):
    response = signup(client, "Test Person", "test@example.com")
    assert response.status_code == 201
    assert response.get_json()["user"] == {
        "email": "test@example.com",
        "fullName": "Test Person",
    }

    with sqlite3.connect(app.config["DATABASE"]) as connection:
        stored_hash = connection.execute(
            "SELECT password_hash FROM users WHERE email = ?",
            ("test@example.com",),
        ).fetchone()[0]
    assert stored_hash != "A-secure-test-password-123"
    assert stored_hash.startswith("scrypt:")
    assert signup(client, "Test Person", "test@example.com").status_code == 409


def test_signup_requires_valid_email_and_at_least_six_character_password(client):
    response = client.post(
        "/api/signup",
        json={"fullName": "Test Person", "email": "invalid", "password": "short"},
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 400
    assert "valid email" in response.get_json()["error"]

    response = client.post(
        "/api/signup",
        json={
            "fullName": "Test Person",
            "email": "test@example.com",
            "password": "12345",
        },
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 400
    assert "6" in response.get_json()["error"]

    response = client.post(
        "/api/signup",
        json={
            "fullName": "Test Person",
            "email": "test@example.com",
            "password": "123456",
        },
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 201


def test_login_rejects_invalid_password_and_accepts_correct_password(client):
    signup(client, "Test Person", "test@example.com")
    client.post("/api/logout", headers={"X-CSRFToken": csrf(client)})

    response = client.post(
        "/api/login",
        json={"email": "test@example.com", "password": "incorrect-password"},
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 401

    response = client.post(
        "/api/login",
        json={"email": "TEST@example.com", "password": "A-secure-test-password-123"},
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 200
    assert response.get_json()["user"]["email"] == "test@example.com"
    assert client.get("/api/me").get_json()["user"]["fullName"] == "Test Person"
    dashboard = client.get("/birth-details.html")
    assert dashboard.status_code == 200
    assert b"Update Personal Details" in dashboard.data
    assert b'id="personalDetailsLink"' in dashboard.data
    assert b'id="region" name="region"' in dashboard.data
    assert b"Please enter your state of birth." in dashboard.data
    for label in (
        b"Horoscope / Kundli Chart",
        b"Documents",
        b"Wallet",
        b"Astrology Services",
        b"Consultations",
        b"Puja History",
        b"Payment History",
    ):
        assert label in dashboard.data


def test_login_survives_app_restart_with_persistent_database(client, app):
    signup(client, "Persistent User", "persistent@example.com", "A-secure-test-password-123")

    restarted_app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": app.config["SECRET_KEY"],
            "DATABASE": app.config["DATABASE"],
            "DOCUMENTS_DIRECTORY": app.config["DOCUMENTS_DIRECTORY"],
            "GEOCODER_USER_AGENT": app.config["GEOCODER_USER_AGENT"],
            "WTF_CSRF_ENABLED": True,
        }
    )
    restarted_client = restarted_app.test_client()
    response = restarted_client.post(
        "/api/login",
        json={
            "email": "persistent@example.com",
            "password": "A-secure-test-password-123",
        },
        headers={"X-CSRFToken": csrf(restarted_client)},
    )

    assert response.status_code == 200
    assert response.get_json()["user"] == {
        "email": "persistent@example.com",
        "fullName": "Persistent User",
    }
    for path in (
        "/astro-essentials.html",
        "/exclusive-yantra.html",
        "/kundli-report.html",
        "/premium-gemstone.html",
        "/rashi-yantra.html",
        "/standard-gemstone.html",
    ):
        assert client.get(path).status_code == 200


def test_csrf_is_required_for_mutations(client):
    response = client.post(
        "/api/signup",
        json={
            "fullName": "Test Person",
            "email": "test@example.com",
            "password": "A-secure-test-password-123",
        },
    )
    assert response.status_code == 400
    assert "verified" in response.get_json()["error"]


def test_login_and_signup_attempts_are_rate_limited(client):
    for _ in range(5):
        response = client.post(
            "/api/login",
            json={"email": "missing@example.com", "password": "invalid-password"},
            headers={"X-CSRFToken": csrf(client)},
        )
        assert response.status_code == 401
    response = client.post(
        "/api/login",
        json={"email": "missing@example.com", "password": "invalid-password"},
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 429

    for index in range(10):
        response = client.post(
            "/api/signup",
            json={
                "fullName": f"Test Person {index}",
                "email": f"person-{index}@example.com",
                "password": "A-secure-test-password-123",
            },
            headers={"X-CSRFToken": csrf(client)},
        )
        assert response.status_code == 201
    response = client.post(
        "/api/signup",
        json={
            "fullName": "Another Person",
            "email": "another@example.com",
            "password": "A-secure-test-password-123",
        },
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 429


def test_birth_details_are_validated_and_scoped_to_the_signed_in_user(client, app):
    signup(client, "First Person", "first@example.com")
    missing_state = valid_birth_details()
    missing_state["region"] = ""
    response = client.put(
        "/api/profile",
        json=missing_state,
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 400
    assert "State" in response.get_json()["error"]

    invalid = valid_birth_details()
    invalid["dateOfBirth"] = (date.today().replace(year=date.today().year + 1)).isoformat()
    response = client.put(
        "/api/profile",
        json=invalid,
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 400
    assert "future" in response.get_json()["error"]

    invalid_time = valid_birth_details()
    invalid_time["timeOfBirth"] = "25:00"
    response = client.put(
        "/api/profile",
        json=invalid_time,
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 400
    assert "valid time" in response.get_json()["error"]

    unknown_time = valid_birth_details()
    unknown_time["birthTimeUnknown"] = True
    unknown_time["timeOfBirth"] = ""
    response = client.put(
        "/api/profile",
        json=unknown_time,
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 200
    assert response.get_json()["details"]["birthTimeUnknown"] is True
    assert response.get_json()["details"]["timeOfBirth"] == ""

    second_client = app.test_client()
    assert second_client.get("/api/profile").status_code == 401
    signup(second_client, "Second Person", "second@example.com")
    assert second_client.get("/api/profile").get_json()["details"] is None

    with sqlite3.connect(app.config["DATABASE"]) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM birth_details").fetchone()[0]
    assert row_count == 1


def test_profile_name_changes_are_reflected_in_the_authenticated_account(client):
    signup(client, "Original Name", "profile-name@example.com")
    response = client.put(
        "/api/profile",
        json=valid_birth_details(name="Updated Name"),
        headers={"X-CSRFToken": csrf(client)},
    )

    assert response.status_code == 200
    assert response.get_json()["details"]["fullName"] == "Updated Name"
    assert client.get("/api/me").get_json()["user"] == {
        "email": "profile-name@example.com",
        "fullName": "Updated Name",
    }


def test_kundli_request_uses_saved_profile_and_is_not_reported_as_confirmed(client, app):
    signup(client, "Test Person", "test@example.com")
    headers = {"X-CSRFToken": csrf(client)}
    response = client.post(
        "/api/kundli-requests",
        json={"message": "Career guidance"},
        headers=headers,
    )
    assert response.status_code == 400

    client.put(
        "/api/profile",
        json=valid_birth_details(),
        headers={"X-CSRFToken": csrf(client)},
    )
    response = client.post(
        "/api/kundli-requests",
        json={"message": "Career guidance"},
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 201
    assert response.get_json()["status"] == "pending"
    assert "no appointment or report is confirmed" in response.get_json()["message"]

    with sqlite3.connect(app.config["DATABASE"]) as connection:
        snapshot = connection.execute(
            "SELECT birth_details_snapshot FROM kundli_requests"
        ).fetchone()[0]
    assert '"placeOfBirth": "Jaipur"' in snapshot


def test_private_files_are_not_served_and_original_svg_is_available(client):
    svg = client.get("/talk2jyotisha-logo.svg")
    assert svg.status_code == 200
    assert svg.mimetype == "image/svg+xml"
    assert b"<svg" in svg.data
    assert client.get("/app.py").status_code == 404
    assert client.get("/requirements.txt").status_code == 404


def test_member_portal_apis_require_authentication(client):
    for path in (
        "/api/dashboard",
        "/api/charts",
        "/api/documents",
        "/api/wallet",
        "/api/pujas",
    ):
        assert client.get(path).status_code == 401


def test_vercel_auth_fails_clearly_without_persistent_database(client, app):
    app.config["VERCEL_DEPLOYMENT"] = True
    headers = {"X-CSRFToken": csrf(client)}

    signup_response = client.post(
        "/api/signup",
        json={
            "fullName": "Test Person",
            "email": "test@example.com",
            "password": "A-secure-test-password-123",
        },
        headers=headers,
    )
    login_response = client.post(
        "/api/login",
        json={"email": "test@example.com", "password": "A-secure-test-password-123"},
        headers=headers,
    )

    assert signup_response.status_code == 503
    assert login_response.status_code == 503
    assert "DATABASE_URL" in signup_response.get_json()["error"]


def test_unavailable_postgres_does_not_take_down_public_site(client, app, monkeypatch):
    app.config["DATABASE_URL"] = "postgresql://user:password@db.example.invalid/app"
    app.config["DATABASE_INITIALIZED"] = False
    app.config["VERCEL_DEPLOYMENT"] = True

    def fail_connect(*_args, **_kwargs):
        raise app_module.psycopg.OperationalError("database unavailable")

    monkeypatch.setattr(app_module.psycopg, "connect", fail_connect)

    assert client.get("/").status_code == 200
    assert client.get("/site-auth.js").status_code == 200
    response = client.post(
        "/api/signup",
        json={
            "fullName": "Test Person",
            "email": "test@example.com",
            "password": "A-secure-test-password-123",
        },
        headers={"X-CSRFToken": csrf(client)},
    )

    assert response.status_code == 503
    assert "DATABASE_URL" in response.get_json()["error"]


def test_postgres_sql_translation_preserves_ids_and_placeholders():
    schema = app_module._postgres_sql(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE
        );
        CREATE TABLE birth_details (
            user_id INTEGER PRIMARY KEY REFERENCES users(id)
        );
        """
    )
    query = app_module._postgres_sql(
        "SELECT id FROM users WHERE email = ? COLLATE NOCASE"
    )

    assert "id BIGSERIAL PRIMARY KEY" in schema
    assert "user_id BIGINT PRIMARY KEY REFERENCES users(id)" in schema
    assert "COLLATE NOCASE" not in schema
    assert query == "SELECT id FROM users WHERE email = %s "
    assert app_module._postgres_sql("BEGIN IMMEDIATE") == "BEGIN"
    seed_query = app_module._postgres_sql(
        """
        INSERT INTO wallet_accounts (user_id, balance_cents, updated_at)
        SELECT id, 0, ? FROM users
        WHERE TRUE
        ON CONFLICT (user_id) DO NOTHING
        """
    )
    assert "FROM users\n        WHERE TRUE\n        ON CONFLICT" in seed_query


def test_unknown_api_route_returns_json_instead_of_html(client):
    response = client.get("/api/not-a-real-endpoint")
    assert response.status_code == 404
    assert response.is_json
    assert response.get_json()["error"] == "The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again."


def test_birth_chart_is_calculated_saved_as_private_pdf_and_reused(client, app, monkeypatch):
    signup(client, "Chart User", "chart@example.com")
    headers = {"X-CSRFToken": csrf(client)}
    client.put("/api/profile", json=valid_birth_details(), headers=headers)
    monkeypatch.setattr(
        app_module,
        "_birthplace_location",
        lambda connection, details, user_agent: {
            "latitude": 26.9124,
            "longitude": 75.7873,
            "timezone": "Asia/Kolkata",
        },
    )

    generated = client.post("/api/charts/generate", json={}, headers=headers)
    assert generated.status_code == 201
    chart = generated.get_json()["chart"]
    assert chart["engine"].startswith("Skyfield JPL DE421")
    assert chart["moonSign"]
    assert chart["ascendant"]["sign"]
    assert len(chart["houses"]) == 12
    assert len(chart["navamsa"]["houses"]) == 12
    assert chart["interpretationAvailable"] is False
    assert chart["documentId"]
    assert generated.get_json()["document"]["category"] == "kundli"

    repeated = client.post("/api/charts/generate", json={}, headers=headers)
    assert repeated.status_code == 200
    assert repeated.get_json()["reused"] is True
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM birth_charts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1

    download = client.get(generated.get_json()["document"]["downloadUrl"])
    assert download.status_code == 200
    assert download.mimetype == "application/pdf"
    assert download.data.startswith(b"%PDF")

    document_list = client.get("/api/documents?category=kundli&q=Birth%20Chart")
    assert document_list.status_code == 200
    assert len(document_list.get_json()["documents"]) == 1


def test_chart_documents_and_wallet_are_scoped_to_the_signed_in_user(client, app, monkeypatch):
    signup(client, "First Person", "first@example.com")
    headers = {"X-CSRFToken": csrf(client)}
    client.put("/api/profile", json=valid_birth_details(), headers=headers)
    monkeypatch.setattr(
        app_module,
        "_birthplace_location",
        lambda connection, details, user_agent: {
            "latitude": 26.9124,
            "longitude": 75.7873,
            "timezone": "Asia/Kolkata",
        },
    )
    generated = client.post("/api/charts/generate", json={}, headers=headers)
    chart_id = generated.get_json()["chart"]["chartId"]
    document_id = generated.get_json()["document"]["id"]

    other_client = app.test_client()
    signup(other_client, "Second Person", "second@example.com")
    assert other_client.get("/api/charts").get_json()["charts"] == []
    assert other_client.get(f"/api/charts/{chart_id}").status_code == 404
    assert other_client.get(f"/api/documents/{document_id}/download").status_code == 404
    assert other_client.get("/api/documents").get_json()["documents"] == []
    wallet = other_client.get("/api/wallet").get_json()["wallet"]
    assert wallet["balanceCents"] == 0
    assert wallet["totalAddedCents"] == 0
    assert wallet["transactions"] == []


def test_wallet_does_not_fake_a_credit_without_a_payment_gateway(client):
    signup(client, "Wallet User", "wallet@example.com")
    headers = {"X-CSRFToken": csrf(client)}
    before = client.get("/api/wallet").get_json()["wallet"]
    assert before["balanceCents"] == 0
    assert before["paymentConfigured"] is False

    response = client.post(
        "/api/wallet/topups",
        json={"amountCents": 50000},
        headers=headers,
    )
    assert response.status_code == 503
    assert "verified payment gateway" in response.get_json()["error"]
    assert client.get("/api/wallet").get_json()["wallet"]["balanceCents"] == 0


def test_dashboard_summary_and_puja_history_show_real_empty_state(client):
    signup(client, "Portal User", "portal@example.com")
    overview = client.get("/api/dashboard").get_json()["overview"]
    assert overview["profileComplete"] is False
    assert overview["chartGenerated"] is False
    assert overview["moonSign"] is None
    assert overview["nameRashi"] == {
        "syllable": "po",
        "signs": ["Kanya (Virgo)"],
    }
    assert overview["documentCount"] == 0
    assert overview["completedPujas"] == 0
    assert overview["upcomingConsultations"] is None
    assert overview["walletBalanceCents"] == 0
    assert client.get("/api/pujas").get_json()["pujas"] == []


def test_name_based_rashi_uses_first_syllable_and_reports_ambiguous_names():
    assert name_based_rashi("Rohan Sharma") == {
        "syllable": "ro",
        "signs": ["Tula (Libra)"],
    }
    assert name_based_rashi("Tara") == {
        "syllable": "ta",
        "signs": ["Simha (Leo)", "Tula (Libra)"],
    }
    assert name_based_rashi("चूड़ामणि") == {
        "syllable": "चू",
        "signs": ["Mesha (Aries)"],
    }
    assert name_based_rashi("Smith") is None


def test_chart_generation_rejects_missing_or_inexact_birth_time(client):
    signup(client, "Incomplete User", "incomplete@example.com")
    headers = {"X-CSRFToken": csrf(client)}
    missing_profile = client.post("/api/charts/generate", json={}, headers=headers)
    assert missing_profile.status_code == 400

    unknown_time = valid_birth_details()
    unknown_time["birthTimeUnknown"] = True
    unknown_time["timeOfBirth"] = ""
    client.put("/api/profile", json=unknown_time, headers=headers)
    response = client.post("/api/charts/generate", json={}, headers=headers)
    assert response.status_code == 400
    assert "exact time" in response.get_json()["error"]


def test_chart_generation_requires_geocoder_configuration(client, app):
    signup(client, "Geocoder User", "geocoder@example.com")
    client.put(
        "/api/profile",
        json=valid_birth_details(),
        headers={"X-CSRFToken": csrf(client)},
    )
    app.config["GEOCODER_USER_AGENT"] = ""
    response = client.post(
        "/api/charts/generate",
        json={},
        headers={"X-CSRFToken": csrf(client)},
    )
    assert response.status_code == 503
    assert "T2J_GEOCODER_USER_AGENT" in response.get_json()["error"]


def test_birthplace_timezone_conversion_handles_daylight_saving_edges():
    assert 24.2 < _lahiri_ayanamsa(2451545.0 + 26 * 365.2425) < 24.3
    base = {
        "birthTimeUnknown": False,
        "birthTimeAccuracy": "exact",
        "timeOfBirth": "01:30",
    }
    india = dict(base, dateOfBirth="1990-05-10")
    assert _birth_time_utc(india, "Asia/Kolkata").isoformat() == "1990-05-09T20:00:00+00:00"

    ambiguous = dict(base, dateOfBirth="2021-11-07")
    with pytest.raises(ChartInputError, match="occurred twice"):
        _birth_time_utc(ambiguous, "America/New_York")

    nonexistent = dict(base, dateOfBirth="2021-03-14", timeOfBirth="02:30")
    with pytest.raises(ChartInputError, match="did not occur"):
        _birth_time_utc(nonexistent, "America/New_York")
