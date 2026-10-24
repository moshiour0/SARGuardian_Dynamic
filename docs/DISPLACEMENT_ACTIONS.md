# Displacement Actions integration

`Find Displacements` creates an authenticated, durable `displacement_jobs` record and dispatches `.github/workflows/sarguardian-displacement.yml`. The browser never receives GitHub or callback secrets.

## Required server environment variables

- `DISPLACEMENT_ACTIONS_ENABLED=true`
- `GITHUB_ACTIONS_TOKEN`: fine-grained token limited to **Actions: write** for `GITHUB_ACTIONS_REPOSITORY`.
- `GITHUB_ACTIONS_REPOSITORY`: `owner/repository` containing this workflow.
- `GITHUB_ACTIONS_WORKFLOW=sarguardian-displacement.yml`
- `GITHUB_ACTIONS_REF=main`
- `DISPLACEMENT_CALLBACK_SECRET`: shared HMAC secret; never expose as `VITE_*`.
- `PUBLIC_API_URL`: deployed HTTPS API origin, without a trailing slash.

The existing `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, Firebase service-account configuration and restrictive `CORS_ORIGINS` remain required.

## Required GitHub Actions secrets

- `DISPLACEMENT_CALLBACK_SECRET`: exactly the backend value.
- `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD`: a valid Earthdata Login account with
  access to the ASF/NISAR archive. These values are available only to the runner and
  are used by the pinned source downloader; they must never be returned to the browser.
- `GOOGLE_DRIVE_OAUTH_CLIENT_JSON`: a Google OAuth **web client** credential for the
  dedicated SARGuardian storage account. Do not use a Firebase or service-account
  credential for this value.
- `GOOGLE_DRIVE_REFRESH_TOKEN`: offline OAuth refresh token granted by that storage
  account with Drive upload access.
- `GOOGLE_DRIVE_FOLDER_ID`: the parent folder for per-job result folders.

After scientific validation, the runner creates one folder named with the durable job
UUID and uploads only `result-manifest.json` and checksum-verified outputs. New result
folders must be private: the existing API verifies Firebase ownership and streams a
requested artifact only after rechecking its retained SHA-256 and byte count. Existing
historical links remain available for backwards compatibility, but no new anyone-link
permission is created. It never uploads raw NISAR HDF5 in `derived-only` mode. If Drive
authentication, upload, privacy verification, or remote byte verification fails, the
workflow fails and raw cleanup is blocked.
The workflow runs the version-controlled `Backend/science_adapter/run_displacement.py` adapter. It pins `moshiour0/SARGuardian` at `10639a916c9829850775410b3ed7de62e527770d`, uses exact WKT only, queries ASF separately by family and BETA/PROVISIONAL maturity, and retains catalog/identity evidence. The default policy selects pairs by an in-window **secondary acquisition** date; an optional strict mode requires both dates in the window. The manifest records the policy, selected and excluded variants, source hashes, structural validation and cleanup receipts.

Before reporting success, the workflow requires a version-2 JSON manifest with the dispatched job ID, canonical WKT/options fingerprint, exact dates/source repository/ref, checksummed outputs, validation reports, private Drive IDs and remote receipts. Raw files are deleted only after those receipts pass; `full-lossless` retains a byte-identical HDF5 archive. A zero process exit code is insufficient. The supported result is explicitly AOI-clipped, per-product GUNW/GOFF displacement screening; it is not an arbitrary-AOI time-series or hazard forecast. Named-AOI candidate screening and dynamic-AOI inversion return explicit skipped/insufficient-evidence states. Callbacks have signed, ordered sequence numbers and bind job ID plus request fingerprint; GitHub terminal success without the signed completion callback is recorded as failure, never as science success.

Run [002_displacement_jobs.sql](../Backend/migrations/002_displacement_jobs.sql) after the prior processing-job migration. Keep direct browser access revoked; the trusted backend uses the Supabase service-role key and verifies Firebase ownership before reads or writes.
