# Mandatory SARGuardian Scientific Regression Guard

This repository's Find Displacements workflow must follow the canonical, versioned scientific acceptance contract:

- Canonical guard: [`docs/SCIENTIFIC_REGRESSION_GUARD.md`](https://github.com/moshiour0/SARGuardian/blob/456d64a66c96c6326acba7013d33514df4ae9dc7/docs/SCIENTIFIC_REGRESSION_GUARD.md)
- Immutable science engine revision audited with the guard: `456d64a66c96c6326acba7013d33514df4ae9dc7`
- Backend dashboard/aggregator builder used by this feature branch: `ecd125f68ed61c276406f73a1d77eb27979e204e`

**Every AI, contributor, or reviewer must read the canonical guard before changing this repository's workflow, frontend integration, API dispatch, data-retention contract, or scientific presentation.** Do not paste a stale copy of the guard into this repository.

## Separation of product features

- **Find Displacements:** runs target-free over the entire user-submitted AOI and selected date window. The user must not be required to preselect a crown/landslide polygon or target point. Candidate regions are screening leads, not confirmed landslides, hazard detections, forecasts, or warnings.
- **Known Place Analysis:** a separate, later deep-analysis workflow for a region selected/found by the user. Its target-local reference, floor, and point-measurement prerequisites must not block target-free AOI scanning.
- GUNW LOS measurements and GOFF pixel-offset anomalies are distinct measurement families and must not be merged into a single unqualified displacement or 3-D velocity result.

## Integration contract

Every workflow dispatch must bind the same immutable science-engine revision in the manifest, worker checkout, reader, validator, and downstream stage. It must also bind the exact dashboard/aggregator revision used to expose target-free candidates. A SHA displayed in a report is not proof unless the corresponding checkout was verified at runtime.

After any edit, run the repository regression suite, scientific engine regression suite, workflow YAML/JavaScript syntax checks, and the retained-artifact AOI-wide smoke test. Raw product replay/download is not allowed merely to validate this code change; use previously retained data unless a separate explicit raw-replay authorization is supplied.

Never modify or merge into `main` without separate explicit owner authorization. A green software test is not independent geophysical validation; disclose any unresolved measurement, reference, uncertainty, grid, mask, or real-data validation limitation.
