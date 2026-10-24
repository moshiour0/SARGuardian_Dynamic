# Moshiour integration audit

Audit date: 2026-10-24. Final application remains
`https://github.com/shourov-04/SARGuardian_Dynamic`. Work starts at target
`f13730e`, on `feat/moshiour-science-integration`. The local `deployment-fork`
remote points to Shourov; `origin` points to Sagormodak's fork. No remote or
production branch is changed and no live processing is dispatched by this work.

## Inspected revisions and licensing

| Source | Verified revision | Decision |
| --- | --- | --- |
| SARGuardian-backend main | `2218dfeb73353080d8d94bd733d9e7bd9a848886` | Read actual tools, application, tests and workflows; architecture reference only |
| SARGuardian-backend feat/final-aoi-science-integration | `830b054e0f60c4957d64c289e49dea86ff808803` | Dynamic discovery, streaming, retention and diagnostics design reference |
| SARGuardian dynamic engine | `10639a916c9829850775410b3ed7de62e527770d` | Already pinned by target; retain without algorithm upgrades |
| SARGuardian fixed baseline | `ab547ecc82bcc4d6ccef545a59852536c2beacc6` | Separate source-AOI regression reference, never arbitrary-AOI truth |

The backend reference has no tracked LICENSE, COPYING or NOTICE. Its code is
not vendored or copied. New target orchestration is independently written from
audited behavior and public data contracts. The scientific engine is MIT,
copyright 2026 Moshiour Rahman Sarker; its LICENSE remains in the pinned checkout.
Main is richer than the named integration branch (extended science, archive audit
and source-run resolver are absent on the latter). Branch names are not evidence
of scientific completeness.

## Target inventory and baseline

The tracked tree was enumerated including React/TypeScript UI, map geometry,
Firebase authentication, FastAPI routes, Supabase persistence, migrations 001–003,
science adapter, Drive publisher, workflow, deployment files, tests and docs.
The source reference's app/auth/database/session services are architecturally
incompatible with the target and will not be adopted.

* Backend API/auth/config/persistence baseline: 65 passed; one existing
  Starlette/httpx deprecation warning.
* Frontend baseline: 10 test files / 65 tests passed, lint passed, production
  build passed (63 modules). esbuild required execution outside Windows sandbox.
* Science/storage baseline initially encountered Windows temporary-directory
  permission errors; a clean authorized rerun is recorded in the final report.
* Existing science tests use fake byte strings as TIFFs and mocked downloads;
  they establish orchestration regression behavior, not geophysical validation.

## Comparison, mapping and priorities

| Capability / source reference | Existing target | Adaptation and target location |
| --- | --- | --- |
| `tools/discover_aoi_products.py`, `tools/time_window.py` | `app/asf_catalog.py` handles only one JSON shape; adapter uses engine reconnaissance; reference/secondary/maturity provenance incomplete | Shared identity/discovery policy in `science_adapter/catalog.py`; API catalog normalization; strict completeness, exact WKT, inclusive secondary default and optional both-acquisition mode |
| Dynamic WKT docs and engine `aoi_wkt.py` | API regex/range checks allow invalid rings; map supplies exact coordinates | Shared lightweight `science_adapter/geometry.py` used before dispatch and processing; topology/size validation, canonical hash, no fallback AOI |
| `tools/download_nisar_product.py`, streaming workflow | One reader invocation at a time, but unconditional deletion in `finally` and workflow `always()` | Rewrite `run_displacement.py` lifecycle: deterministic manifest, preflight, retries, raw SHA/inventory, validated outputs, remote verification, cleanup receipts/checkpoints |
| `tools/verify_derived_product_outputs.py`, `validate_aoi_derived_outputs.py` | Output existence/hash described as validation | `science_adapter/validation.py`: HDF5 identity/inventory, exact reader TIFF schemas/metadata/grid/AOI/quality/numeric validation, CSV parity to retained pixels |
| Archive / reader-equivalence tools | No retention choice or raw dependency audit | Default derived-only; optional full-lossless byte-identical original HDF5 archive with hashes and reader parity before cleanup; never call derived-only unrestricted retention |
| `tools/drive_resumable_upload.py` | Local hash only, multipart upload, public anyone-reader folder | Extend existing `upload_to_drive.py` with quota, resumable bounded uploads, remote hash/bytes checks, private folders, verified receipts; owner-authorized downloads through existing API |
| `tools/run_export_science_diagnostics.py`, engine `pixel_stack.py`, `candidate_check.py` | No advanced custom-AOI diagnostics | Retained-raster diagnostics with explicit temporal/geometry support; source-specific candidates return `SKIPPED_UNSUPPORTED_CUSTOM_AOI`; no fabricated trend or negative detection |
| Fixed-source aggregation / timeseries / atmosphere / bounds | Deliberately per-product only | Audit and explicitly gate unsupported dynamic stages; keep family, track, layer, maturity and datum separate; no per-pair-median velocity claim |
| Retry/resume/aggregation tools | No product receipts or checkpoint compatibility | Verify request, science/pipeline/output-contract fingerprints and local/remote hashes; block incomplete aggregation; continue eligible work while preserving evidence |
| Source worker callbacks | Target HMAC + ordered sequence + owner API already present | Preserve target flow; bind job identity, atomic callback sequence, enforce v2 scientific/storage evidence; retain legacy completed records |
| Source standalone dashboard | Target React results ignore `result_summary` | Extend current `DisplacementPreparation.tsx` and typed API only; product options, science status, provenance and authorized downloads, real previews where produced |

## Security and deployment decisions

Preserve Firebase, Supabase, existing job tables, Vercel entrypoints, Drive account,
workflow dispatch and signing. Add only immutable scientific option columns and
admission/concurrency constraints where needed. Historical artifact URLs remain
readable. New outputs must stay private: existing anyone-link behavior does not
satisfy owner isolation. Do not put owner credentials, tokens, signed download URLs
or external exception bodies into manifests or public Actions artifacts.

Workflow remains manual/dispatch-only. Ordinary pushes run bounded fixture tests,
never baseline downloads. Use a small validation pilot by default, finite catalog,
product/disk/download/storage budgets, bounded retry and one raw HDF5 at a time.
Failed retention must stop raw cleanup. Hosted runners are ephemeral: retaining a
failed local input is not a promise of durable raw preservation after runner exit.

## Scientific decisions and limits

ASF documents GUNW/GOFF as paired-acquisition products and `dataMaturity` filters
as the BETA/PROVISIONAL classification. Catalog timestamps alone do not establish
both acquisition dates. Query from 2024 through inclusive end to retain pairs with
reference before requested start, then apply exact parsed reference/secondary policy.
Reject incomplete catalog evidence rather than silently truncate.

Deduplicate reprocessed releases only within an explicit observation/processing
identity; preserve PR/UR distinctions needed by future inversion and report every
superseded release. Do not transplant fixed-baseline deduplication rules into a
different inversion. Release and datum differences prevent indiscriminate stacking.

The pinned readers consume raw HDF5 for phase/ionosphere/offset/quality masks,
reference selection and product-native geometry. Derived-only retains the enabled
per-product rasters, quality bands, numeric summaries, inventory, hashes and receipts;
it cannot support unrestricted later LOS/atmosphere/raw-reference reprocessing.
Source candidate checking uses named-AOI points; source diagnostics also contain
date-only filename assumptions. These must not silently apply to user polygons.

Structural validation means schema/grid/identity/numeric/storage evidence, not
independent geophysical accuracy. A pilot is an operational check. Neither a PASS,
candidate, inverse-velocity result nor workflow success is a landslide prediction.
Any advanced stage lacking common datum, connected temporal support or custom-AOI
validation returns an explicit unsupported/insufficient-evidence status.

Primary contracts checked: [ASF naming](https://nisar-docs.asf.alaska.edu/naming-conventions/),
[ASF maturity search](https://nisar-docs.asf.alaska.edu/asf-search/),
[ASF availability and release caveats](https://nisar-docs.asf.alaska.edu/availability-overview/).

Implementation follows this audit; final evidence and remaining limits are recorded
separately in `MOSHIOUR_INTEGRATION_REPORT.md` and the controlled pilot runbook.
