# Talk2Jyotisha

## Run locally

From this folder, install the Python dependencies and start the application:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). Do not use a static file server or open the HTML files directly: authentication and protected-page checks are handled by the Python server.

The server creates `.t2j-private\secret.key` and `.t2j-private\talk2jyotisha.sqlite3` on first run. Keep this directory private and backed up; it contains the session signing key and account/profile data. It is excluded from source-control and should not be included in project archives.

## Member portal

The existing member dashboard includes personal birth details, saved birth charts, private PDF documents, wallet and transaction history, consultation links, puja history, and astrology services. New tables are created with non-destructive `CREATE TABLE IF NOT EXISTS` statements; existing accounts and birth profiles are preserved. Generated chart PDFs are stored in `.t2j-private\documents`, never in the public static directory, and downloads are checked against the signed-in customer.

Chart generation uses the identifying OpenStreetMap Nominatim user agent `Talk2Jyotisha/1.0` by default. You can override it before starting the server, for example to include a contact address:

```powershell
$env:T2J_GEOCODER_USER_AGENT = "Talk2Jyotisha/1.0 (contact: you@example.com)"
python app.py
```

Chart generation geocodes only the saved city, state, and country (not the customer's name or date of birth). It resolves the birthplace time zone using the geocoded coordinates, then calculates planetary positions using Skyfield and the bundled JPL DE421 ephemeris. The implementation uses Lahiri sidereal conversion and whole-sign houses; its ayanamsa is approximate, and calculations are limited to birth dates from 1900 through 2050. Exact birth time is required. Generated charts are persisted and reused for the same birth profile. Reports are saved as private PDFs.

AI interpretation is not configured, so the portal reports that interpretation is unavailable rather than generating predictions. No payment gateway is configured; wallet balances are server-owned and remain zero unless a real payment integration and verified webhook are implemented. Deposits, service spending, and payment confirmations are therefore unavailable, not simulated. Puja history is empty until a trusted booking source writes records. Consultation appointments continue to use the existing booking page and are not represented as confirmed dashboard bookings.

## Authentication and deployment

- Accounts and birth details use SQLite locally and PostgreSQL when `DATABASE_URL` is configured. Passwords are hashed; browser storage is not used for authentication.
- Local development uses a generated signing key. For deployment, set `T2J_ENV=production`, set `T2J_SECRET_KEY` to a unique, randomly generated secret, and set `DATABASE_URL` to a persistent PostgreSQL connection string. Production cookies require HTTPS.
- Use a production WSGI server and HTTPS-enabled hosting. The built-in Flask development server is for local development only.
- Vercel detects the Flask application from `index.py`. Configure `T2J_SECRET_KEY` and `DATABASE_URL` in the Vercel project environment variables; use a managed PostgreSQL provider such as Supabase or Neon. Vercel's `/tmp` storage is temporary and instance-local, so SQLite and generated PDFs are not persistent there. PostgreSQL keeps accounts and profiles available across instances; generated PDFs still need a persistent object/file store. Existing SQLite accounts are not migrated automatically and must sign up again after PostgreSQL is configured.
- Email verification, password recovery, email delivery, and payment processing are not configured. Existing browser-only accounts cannot be migrated safely; users must sign up again.

## Kundli report request

The site lists a 15-minute consultation at **$21**, but it has no configured price for a bundled detailed Kundli report and 15-minute consultation. The new request page deliberately does not invent a package price. It stores a pending request and the birth-profile snapshot; submitting the request does not confirm an appointment or report.
