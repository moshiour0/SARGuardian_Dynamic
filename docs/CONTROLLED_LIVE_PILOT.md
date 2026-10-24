# Controlled live science pilot

Run this only after migration `004_scientific_request_contract.sql` has been applied,
the target repository workflow has been reviewed, and authorized Earthdata and private
Drive credentials have been configured. The normal test suite uses deterministic mocks;
it does not establish geophysical accuracy.

1. Create a small, closed EPSG:4326 polygon with known NISAR L2 coverage and an inclusive
   window of at most 90 days. Start with `product_type=BOTH`,
   `processing_profile=validation-pilot`, `retention_mode=derived-only`, and
   `acquisition_window=secondary-acquisition-in-window`.
2. Submit through the signed-in website. Confirm the job has a new immutable option set,
   Actions run ID, and signed running callbacks.
3. Inspect the result manifest. Confirm exact AOI hash, source/secondary dates, catalog
   maturity evidence, release exclusions, source HDF5 hashes/inventory, raster validation
   reports, and private Drive remote receipts. Check each GeoTIFF in a GIS before relying
   on it for research.
4. Confirm that the API owner download works and a different user receives no artifact.
   Confirm Drive contains no public `anyone` permission. Do not expose direct Drive IDs.
5. Confirm the `raw_cleanup_verified` stage follows, rather than precedes, every validated
   remote receipt. For a full-lossless pilot, additionally verify archive SHA-256 equals
   the original HDF5 and retain the archive receipt.
6. Record the run ID, configured science commit, manifest hash, data maturity caveats and
   observed limitations in the study log. Do not label a structural PASS, a candidate, or
   an inverse-velocity screen as a landslide prediction.

Stop and investigate on any missing catalog evidence, empty AOI intersection, download
identity mismatch, raster validation failure, failed remote receipt, callback mismatch,
or a `SKIPPED`/`INSUFFICIENT_DATA` science stage. Never rerun a broad baseline from a
normal push; expand to `full-aoi` only after a reviewed pilot and a documented resource
budget.
