# Moshiour integration implementation report

Implementation branch: `feat/moshiour-science-integration`.

## Operational additions

The target application now keeps its React UI, Firebase owner authentication, Supabase
job model, signed GitHub callback and Vercel layout while adding immutable product,
window, retention and profile options. It validates closed simple EPSG:4326 AOIs and
binds a canonical request fingerprint to the workflow callback.

The worker independently adapts the audited source patterns: exact dynamic-AOI ASF
discovery by product family and maturity, deterministic product/variant evidence,
bounded download retries, source HDF5 inventory/hash, per-product GUNW/GOFF export,
retained-raster/CSV validation, provenance, Drive byte/MD5 verification, and explicit
cleanup receipts. The Actions path publishes and verifies each product checkpoint before
the next raw HDF5 download, then applies the cleanup gate. New artifacts remain private
and owner-downloadable through the existing API. The UI presents status, validation scope, products, provenance, warnings,
diagnostics, private downloads and produced previews.

## Evidence

Local deterministic verification completed: backend suite 86 passed (one existing
Starlette/httpx deprecation warning); frontend suite 65 passed; frontend lint/build
passed; Python compiled the backend/science modules; pinned HDF5/raster runtime imports;
workflow YAML parsing; `git diff --check`. No authenticated ASF, Earthdata, Google Drive,
Supabase, GitHub Actions or live satellite processing was run.

## Scientific scope and remaining limits

Operationally supported: real product discovery/processing when credentials and suitable
data exist, AOI-clipped per-product GUNW/GOFF outputs, structural/provenance/storage
validation, and lossless byte-identity archive retention when selected.

Scientifically validated here: input/identity/format/hash/retention contracts against
small deterministic fixtures only. This is not a live geophysical validation.

Explicitly unsupported for arbitrary AOIs: named-AOI candidate screening, a common-datum
time-series, velocity/inverse-velocity claims, and unrestricted future raw-data analysis
from derived-only outputs. They are reported as skipped or insufficient evidence rather
than calculated. Use `CONTROLLED_LIVE_PILOT.md` before enabling real work.
