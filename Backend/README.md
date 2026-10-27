# SARGuardian catalogue API foundation

This FastAPI service provides the frontend's metadata-only archive preflight.
It queries the official ASF Search API for NISAR `GUNW` and `GOFF` metadata only. It does not
download satellite data, run scientific processing, or fabricate catalogue products.
Set `ASF_CATALOG_ENABLED=false` to disable the adapter; otherwise valid searches use the real
ASF endpoint and return `ready` or `no_data`.

## Setup

Use Python 3.11+ in this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

During local development, the backend loads `Backend/.env` through `python-dotenv`.
Existing host environment variables take precedence. Set `APP_ENV=production` in a
hosted environment; production skips local dotenv loading and requires hosting-provided
environment variables, including an explicit `CORS_ORIGINS` value.

Configure Firebase Admin with Application Default Credentials or a server-only
`GOOGLE_APPLICATION_CREDENTIALS` path. On Vercel, use the server-only
`FIREBASE_SERVICE_ACCOUNT_JSON` environment variable instead; it contains the full JSON
object supplied by the hosting environment and is parsed in memory. Do not commit its value,
print it in logs, or place service-account credentials in `Frontend/`
or any `VITE_*` variable. The frontend sends a Firebase ID token as `Authorization: Bearer ...`.
Set `APP_ENV=production` and an explicit comma-separated `CORS_ORIGINS` allowlist in production;
the server refuses to start in production without that allowlist. `PORT` is honored for hosting
platforms and defaults to `8000` locally.

For durable jobs, set `SUPABASE_URL` and the server-only `SUPABASE_SERVICE_ROLE_KEY`.
That variable may contain either a modern opaque `sb_secret_...` key or a legacy
`service_role` JWT. Modern keys are sent to Supabase as `apikey` and are never used
as `Authorization: Bearer ...`; the Firebase ID token remains the separate client
authentication credential.
There is no in-memory fallback: missing values produce a safe configuration error. Apply
`supabase/migrations/20261017143644_processing_jobs.sql` to the Supabase project before enabling job creation.
Supabase does not automatically treat Firebase identities as Supabase Auth identities. The
backend verifies Firebase first and filters every persistence operation by that verified UID;
the SQL RLS policies are defense-in-depth for a future Supabase JWT integration.
The manual, approval-gated staging procedure is documented in
[`STAGING_MIGRATION_RUNBOOK.md`](STAGING_MIGRATION_RUNBOOK.md). It explicitly warns against
selecting the old paused project and does not authorize this local environment to contact
Supabase.

## Render: private Google Drive OAuth client secret file

For the API service that serves `GET /api/displacement-jobs/{job_id}/dashboard`, Render can supply the Google OAuth client JSON as a mounted secret file instead of a multiline environment-variable value.

1. In Render, open the **web service that runs this FastAPI API** (not just a separate worker or a different frontend service).
2. Open **Environment** → **Secret Files** → **+ Add Secret File**.
3. Set **Filename** to `google_drive_oauth_client.json`.
4. Paste the complete downloaded OAuth client JSON into **Contents**, then save changes.
5. Under **Environment Variables**, set:
   - `GOOGLE_DRIVE_OAUTH_CLIENT_JSON_FILE=/etc/secrets/google_drive_oauth_client.json`
   - `GOOGLE_DRIVE_REFRESH_TOKEN` = refresh token minted for this same OAuth client ID
   - `GOOGLE_DRIVE_FOLDER_ID` = the Drive folder ID only (not the entire folder URL)
6. Save and deploy/redeploy the API service. Render mounts service secret files under `/etc/secrets/`.

The JSON must contain a top-level `"web"` object with `client_id`, `client_secret`, and `token_uri`. A Google OAuth Desktop app JSON uses `"installed"` instead and is rejected by this application; create/use a Web application OAuth client and mint the refresh token for that same client. The refresh token must have Drive permissions to read the result files and access the configured private folder.

`GOOGLE_DRIVE_OAUTH_CLIENT_JSON_FILE` takes precedence over `GOOGLE_DRIVE_OAUTH_CLIENT_JSON` when both are set. The API service needs these settings because it downloads a private Drive archive when the user opens the dashboard. Setting them only in GitHub Actions, or only on another Render service, does not configure this API process. Confirm that the frontend's built `VITE_SARGUARDIAN_API_URL` points to the service where these credentials were set.

## Safe local configuration preflight

Do not commit `Backend/.env`. Create it manually from `Backend/.env.example` only after
obtaining staging values. The current code expects these names:

- `APP_ENV` (`development` locally; `production` on a host)
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY` (the server-only modern secret API key, or a legacy service-role JWT)
- `GOOGLE_APPLICATION_CREDENTIALS` (local file path, optional when Application Default Credentials are configured)
- `FIREBASE_SERVICE_ACCOUNT_JSON` (Vercel-only full JSON secret, parsed in memory)
- `ASF_CATALOG_ENABLED`, `ASF_SEARCH_URL`, `ASF_MAX_RESULTS`, and `ASF_TIMEOUT_SECONDS`
- `CORS_ORIGINS` (comma-separated origins; required in production)
- `PREFLIGHT_TTL_SECONDS` (60 through 604800 seconds)
- `DISPLACEMENT_ACTIONS_ENABLED`, `GITHUB_ACTIONS_TOKEN`, `GITHUB_ACTIONS_REPOSITORY`, `GITHUB_ACTIONS_WORKFLOW`, `GITHUB_ACTIONS_REF`, `DISPLACEMENT_CALLBACK_SECRET`, and `PUBLIC_API_URL` for durable scientific-processing dispatch
- Worker-side GitHub Actions secrets: `EARTHDATA_USERNAME`, `EARTHDATA_PASSWORD` or `EARTHDATA_TOKEN`, `GOOGLE_DRIVE_OAUTH_CLIENT_JSON`, `GOOGLE_DRIVE_REFRESH_TOKEN`, and `GOOGLE_DRIVE_FOLDER_ID`
- API runtime secrets (required on the Render `SARGuardian_Dynamic` service, and on any Vercel FastAPI deployment serving the dashboard): `GOOGLE_DRIVE_OAUTH_CLIENT_JSON`, `GOOGLE_DRIVE_REFRESH_TOKEN`, and `GOOGLE_DRIVE_FOLDER_ID`
- `PORT` (optional; defaults to 8000)

Manual setup:

1. Copy `Backend/.env.example` to `Backend/.env` locally; do not commit the copy.
2. Set `APP_ENV=development` and the staging `SUPABASE_URL`.
3. Put the modern server-only Supabase secret key in `SUPABASE_SERVICE_ROLE_KEY`.
4. Set either `GOOGLE_APPLICATION_CREDENTIALS` to a local server-side credential path, or configure Application Default Credentials. Do not put the credential JSON in this repository.
5. Set `ASF_CATALOG_ENABLED` deliberately, review `CORS_ORIGINS`, and keep `PREFLIGHT_TTL_SECONDS` within its supported range.
6. From `Backend`, run the non-networking diagnostic:

```powershell
python -m app.config_diagnostic
```

The diagnostic reports only variable names and shape/status messages. It does not initialize
Supabase or Firebase, contact ASF or Supabase, apply migrations, print values or key prefixes,
or read the Firebase credential file. It exits with code 0 only when all checks pass. The
Supabase secret is an API key for the server-side `apikey` header; it is not a Firebase token
and is never the end-user `Authorization: Bearer` credential.

## Private dashboard API storage requirements

The GitHub Actions worker uploads the completed `aoi-derived-results.zip` bundle to private Google Drive and sends a checksum-verified Drive receipt to the API. The API then downloads that same private file only after confirming the signed-in user owns the succeeded job. **The API runtime must have its own Drive OAuth configuration as well as the worker.** GitHub Actions secrets do not automatically become Render service environment variables.

On the Render service `SARGuardian_Dynamic` (or the FastAPI hosting environment), add these three server-only environment variables using the same values as the workflow's GitHub Actions secrets:

- `GOOGLE_DRIVE_OAUTH_CLIENT_JSON`: the OAuth client JSON object, including its `web` credentials.
- `GOOGLE_DRIVE_REFRESH_TOKEN`: the OAuth refresh token for the Drive identity that can access the private result folder.
- `GOOGLE_DRIVE_FOLDER_ID`: the parent folder ID used by the worker.

Keep the values private: do not commit them, put them in any `VITE_*` variable, or paste them into issues or logs. After saving the variables, redeploy/restart the API service and retry opening the existing job's dashboard; the existing ZIP is already retained, so a new NISAR processing run is not needed just to retry the dashboard. If the 503 remains, check the API's safe server log message for the sanitized Google Drive failure reason (missing configuration, OAuth authentication, or file retrieval failure).

## Vercel deployment preparation

Create two Vercel projects from this repository without deploying from this phase:

- Frontend project root: `Frontend/`
- Backend project root: `Backend/`

For the backend project, use the Python runtime with `index.py` as the FastAPI entrypoint.
Vercel installs the backend dependencies from `Backend/requirements.txt`; no separate server
or long-running process is required. Set `APP_ENV=production` and an explicit trusted
comma-separated `CORS_ORIGINS` value containing the final Vercel frontend origin. Also set
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `FIREBASE_SERVICE_ACCOUNT_JSON` in Vercel's
server-side environment settings. Do not set `GOOGLE_APPLICATION_CREDENTIALS` to a Windows
path on Vercel. Apply the timestamped SQL files in `supabase/migrations/` in ascending timestamp order through `20261027123456_place_target_contract.sql` before enabling dispatch. This latest migration adds immutable map-place context and optional in-AOI target coordinates; it must be applied before deploying code that writes those fields. The Actions worker retains private, verified results and reports them through the existing owner-scoped displacement API; see [`../docs/DISPLACEMENT_ACTIONS.md`](../docs/DISPLACEMENT_ACTIONS.md) and [`../docs/CONTROLLED_LIVE_PILOT.md`](../docs/CONTROLLED_LIVE_PILOT.md).

For the frontend project, use the Vite preset, root directory `Frontend/`, build command
`npm run build`, and output directory `dist`. Set the public frontend variables documented in
`Frontend/.env.example` before building. The existing `Frontend/vercel.json` provides the SPA
rewrite for client-side routes such as `/landslide`.

## Run

```powershell
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health: `GET http://localhost:8000/health`
Catalogue preflight: `POST http://localhost:8000/api/catalog/search`
Create confirmed queued job: `POST http://localhost:8000/api/jobs`
List current user's jobs: `GET http://localhost:8000/api/jobs`
Inspect current user's job: `GET http://localhost:8000/api/jobs/{job_id}`

The adapter uses ASF's public `services/search/param` endpoint with a WKT `intersectsWith`
polygon, `platform=NISAR`, `processingLevel=GUNW` or `GOFF`, UTC `start`/`end`,
`output=jsonlite`, and a bounded `maxResults` cap. ASF's response has no continuation token;
when the cap is reached, the response sets `truncated=true` and asks the caller to narrow the
search. Catalogue search is read-only and does not require Earthdata download credentials.

## Request

```json
{
  "coordinates": [[85.3, 27.7], [85.301, 27.7], [85.301, 27.701], [85.3, 27.7]],
  "start_date": "2026-08-01",
  "end_date": "2026-08-15",
  "product_type": "GUNW"
}
```

The endpoint validates a closed WGS84 longitude/latitude polygon, positive area, the 1,000 km2
AOI limit, date order, the 90-day limit, and `GUNW`/`GOFF` product types before the adapter
is called. Results preserve actual ASF granule names, product types, dates, direction, path,
processing level, and size metadata where ASF provides them. Missing metadata remains absent.

`POST /api/displacement-jobs` creates an authenticated durable processing request. It stores
an immutable AOI/date/product/window/retention/profile snapshot plus selected place context and
optional in-AOI target coordinates. The target is accepted only when its WGS84 point lies within
the exact submitted polygon and is bound into the request fingerprint. Place context is not a
measurement, and the target is not a stable reference point: cumulative time-series inversion
remains blocked unless the science workflow proves a compatible common datum across the connected
acquisition network. The workflow is dispatched only when server-side Actions settings are enabled,
and completion is accepted only from the bound signed worker callback. Normal catalog preflight
remains metadata-only.

## Tests

```powershell
python -m pytest
```
