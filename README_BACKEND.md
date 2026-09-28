# Upload PDF Platform Backend

This backend is the hardened control-plane and processing plane for your upload app. The desktop program stays as the client, while this server becomes the secure place for authentication, organizations, labs, jobs, logs, and the actual Star, Gmail, WhatsApp, and API upload execution.

## What the secure server does

The server accepts authenticated requests from the desktop app, stores them in the database, and returns status data back to the app.

Typical flow:

1. A one-time bootstrap request creates the first organization and admin.
2. The desktop app logs in and receives a signed bearer token.
3. The app sends authenticated requests to create upload jobs.
4. The API stores the job in PostgreSQL.
5. A worker picks up pending jobs.
6. The worker processes files and updates job/item status.
7. The desktop app loads job progress from the API.

## Project layout

`backend/config.py`
Application settings loaded from environment variables.

`backend/database.py`
SQLAlchemy engine, base model, and database session handling.

`backend/models.py`
Database tables for organizations, users, labs, jobs, job items, and audit logs.

`backend/schemas.py`
Pydantic request and response models used by the API.

`backend/security.py`
Password hashing, password policy, token signing, and auth dependencies.

`backend/services.py`
Business logic for bootstrap, auth, organizations, labs, users, and jobs.

`backend/api.py`
FastAPI routes your desktop app will call.

`backend/middleware.py`
Security middleware for trusted hosts, request size limits, rate limiting, and hardened response headers.

`backend/integrations.py`
Server-side integrations for API upload, Gmail SMTP, WhatsApp, file hosting, and Star.

`backend/worker.py`
Background worker that picks up pending jobs and performs the real external processing.

`backend/main.py`
FastAPI entry point.

## Recommended production shape

- Desktop app: customer-facing client
- FastAPI: API layer
- PostgreSQL: primary database
- Worker: background processing
- Reverse proxy: Nginx or Caddy
- Optional queue: Redis plus Celery/RQ when job volume grows

## Security features included

- Signed bearer-token authentication for every business endpoint
- One-time bootstrap flow protected by `X-Bootstrap-Token`
- Organization-scoped access control so users only see their own data
- Admin-only creation of users and labs
- Authenticated organization-scoped job updates and cancellation
- Trusted-host enforcement and optional HTTPS redirect
- Secure response headers and no-store caching
- Request body size limits
- In-memory per-minute rate limits for auth and API traffic
- Production validation for `SECRET_KEY`, `ALLOWED_HOSTS`, and `HTTPS_ONLY`

## Core tables

- `organizations`: each customer/company
- `users`: login accounts
- `labs`: customer-specific lab records
- `upload_jobs`: one batch or action requested by the app
- `job_items`: file-level status inside a job
- `audit_logs`: timeline and support/debug logs

## How your secure server works in practical terms

When a user clicks something like "Start Star Upload" in the desktop app, the app now submits a secure job to the backend instead of calling third-party services directly. The request includes:

- which lab is selected
- which channel to use (`star`, `gmail`, `whatsapp`, or `api_upload`)
- which files should be processed
- any per-job metadata the worker needs, such as recipient, CSV path, or lab ID

The server derives the organization and requesting user from the bearer token instead of trusting those values from the client.

The server creates a row in `upload_jobs` and one or more rows in `job_items`.

The worker checks for pending jobs, changes them to `running`, performs the server-side integration work, and updates statuses. Every important step can be written to `audit_logs`.

Because of that:

- your history is centralized
- you can support multiple customers
- you can build reports and dashboards
- your desktop app becomes simpler
- third-party secrets can live on the server instead of in the client

## Local development

1. Create a virtual environment.
2. Install packages from `requirements-backend.txt`.
3. Copy `.env.example` to `.env`.
4. Set `DATABASE_URL`.
5. Set a strong `SECRET_KEY` and a one-time `BOOTSTRAP_TOKEN`.
6. Configure any server-side integration secrets you want the worker to own:

- `GMAIL_SENDER_EMAIL` and `GMAIL_SENDER_PASSWORD`
- `WHATSAPP_SECRET` and `WHATSAPP_ACCOUNT`
- `STAR_EMAIL` and `STAR_PASSWORD`
- `UPLOAD_API_URL` if your API upload target changes

7. Run the API with:

```powershell
uvicorn backend.main:app --reload
```

Then open:

- `http://127.0.0.1:8000/api/v1/health`
- `http://127.0.0.1:8000/docs` only if `DEBUG=true` or `SERVE_DOCS=true`

## First-time bootstrap

Before any user exists, create the first organization admin:

```powershell
$headers = @{ "X-Bootstrap-Token" = "your-one-time-bootstrap-token" }
$body = @{
    organization_name = "Star Software Solution"
    admin_email = "admin@example.com"
    admin_full_name = "Platform Admin"
    admin_password = "UseAVeryStrongPassword!123"
} | ConvertTo-Json

Invoke-WebRequest `
    -UseBasicParsing `
    -Uri "http://127.0.0.1:8000/api/v1/auth/bootstrap" `
    -Method Post `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

After bootstrap, remove or rotate `BOOTSTRAP_TOKEN`.

## Login flow

```powershell
$body = @{
    email = "admin@example.com"
    password = "UseAVeryStrongPassword!123"
} | ConvertTo-Json

Invoke-WebRequest `
    -UseBasicParsing `
    -Uri "http://127.0.0.1:8000/api/v1/auth/token" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body
```

Use the returned bearer token in `Authorization: Bearer <token>`.

## Worker behavior

The server starts a background worker thread automatically when `WORKER_ENABLED=true`.

Supported job channels:

- `api_upload`: uploads PDFs to the configured API target
- `gmail`: sends PDF attachments either from a folder batch or CSV-driven batch
- `whatsapp`: hosts files, then sends WhatsApp messages or documents
- `star`: uploads PDFs and CSV/Excel files to Star

You can also process one pending job manually with the admin-only endpoint:

```powershell
Invoke-WebRequest `
    -UseBasicParsing `
    -Uri "http://127.0.0.1:8000/api/v1/worker/process-next" `
    -Method Post `
    -Headers @{ Authorization = "Bearer <admin-token>" }
```

## Current deployment note

The current desktop-to-backend flow sends file paths to the server worker. That works when the backend can access the same filesystem paths as the desktop app, such as a local deployment or a shared drive setup.

For a fully remote deployment, the next step is to move the files themselves into backend-managed storage or direct upload endpoints so the server does not rely on client-side local paths.

## Production checklist

- Put the API behind Nginx or Caddy with real TLS certificates
- Set `ENVIRONMENT=production`
- Set `HTTPS_ONLY=true`
- Set a long random `SECRET_KEY`
- Limit `ALLOWED_HOSTS` to your real domain names
- Use PostgreSQL instead of SQLite
- Keep `SERVE_DOCS=false`
- Remove `BOOTSTRAP_TOKEN` after the first admin is created
- Store third-party secrets only on the backend or in your server secret manager
- Run the backend and worker on a machine that can access the files being processed

## Suggested next milestones

1. Add Alembic migrations.
2. Move file transport to backend-managed uploads or object storage.
3. Add Redis-backed rate limiting for multi-instance deployments.
4. Split the worker into a separate process when job volume grows.
5. Add secret-manager integration for production credentials.
