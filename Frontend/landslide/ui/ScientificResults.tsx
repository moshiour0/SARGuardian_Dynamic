import { useEffect, useState } from 'react';
import type { User } from 'firebase/auth';
import { downloadDisplacementArtifact, type DisplacementJob } from '../../src/api/catalog';
import {
  asRecord, numericValue, readScientificArtifacts, readScientificDiagnostics,
  safeLegacyArtifactUrl, scientificStatusLabel, textValue,
  type ScientificArtifact,
} from '../engine/scientificResults';

function ArtifactDownload({ artifact, jobId, user }: { artifact: ScientificArtifact; jobId: string; user: User }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [previewUrl, setPreviewUrl] = useState('');
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);
  const isPreview = artifact.kind === 'raster_preview' && /\.png$/i.test(artifact.path);
  const download = async (preview: boolean) => {
    setBusy(true);
    setError('');
    try {
      const blob = await downloadDisplacementArtifact(jobId, artifact.index, user);
      if (preview) {
        setPreviewUrl(URL.createObjectURL(new Blob([blob], { type: 'image/png' })));
      } else {
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = artifact.path.split('/').pop() || 'scientific-output';
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      }
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : 'Could not download the artifact.');
    } finally { setBusy(false); }
  };
  return <li className="science-artifact">
    <strong>{artifact.path}</strong>
    <span>{artifact.kind.split('_').join(' ')}{artifact.bytes !== undefined ? ` · ${artifact.bytes.toLocaleString()} bytes` : ''}</span>
    <div className="science-artifact-actions">
      {artifact.downloadable ? <>
        <button className="demo-button" type="button" disabled={busy} onClick={() => void download(false)}>{busy ? 'Loading…' : 'Download'}</button>
        {isPreview && <button className="demo-button" type="button" disabled={busy} onClick={() => void download(true)}>View raster preview</button>}
      </> : <span>Verified download unavailable</span>}
    </div>
    {previewUrl && <figure><img src={previewUrl} alt={`Displacement raster preview: ${artifact.path}`} /><figcaption>Preview of the retained raster. Refer to the GeoTIFF and manifest for units, quality masks and georeferencing.</figcaption></figure>}
    {error && <p role="alert">{error}</p>}
    {artifact.sha256 && <details><summary>SHA-256 checksum</summary><code>{artifact.sha256}</code></details>}
  </li>;
}

export function ScientificResults({ job, user }: { job: DisplacementJob; user: User }) {
  const summary = asRecord(job.result_summary);
  const currentSchema = summary.schema_version === 2;
  const validation = asRecord(summary.validation);
  const provenance = asRecord(summary.provenance);
  const progress = asRecord(summary.progress);
  const diagnostics = readScientificDiagnostics(summary.diagnostics);
  const artifacts = currentSchema ? readScientificArtifacts(summary.artifacts) : [];
  const products = Array.isArray(summary.products) ? summary.products.map(asRecord) : [];
  const warnings = Array.isArray(summary.warnings) ? summary.warnings.filter((value): value is string => typeof value === 'string') : [];
  const legacyUrl = !currentSchema && job.state === 'succeeded' ? safeLegacyArtifactUrl(job.artifact_url) : undefined;
  const completed = numericValue(progress.completed);
  const total = numericValue(progress.total);
  const failed = numericValue(progress.failed);
  const repository = textValue(provenance.science_repository) || textValue(summary.science_source_repository);
  const commit = textValue(provenance.science_commit) || textValue(summary.science_source_ref);
  const aoiHash = textValue(provenance.aoi_sha256) || textValue(summary.aoi_wkt_sha256);

  return <section className="scientific-results" aria-labelledby="scientific-results-title">
    <h2 id="scientific-results-title">Scientific results</h2>
    <p className="research-output-notice">Research output — not a verified hazard warning.</p>
    {total !== undefined && completed !== undefined && <p><b>Product progress:</b> {completed} of {total} completed{failed !== undefined ? ` · ${failed} failed` : ''}</p>}
    <p><b>Scientific status:</b> {scientificStatusLabel(summary.scientific_status)}{textValue(summary.scientific_status) && ` (${summary.scientific_status})`}</p>
    {validation.status === 'PASS'
      ? <p><b>Output validation passed.</b> Structure and provenance checks do not establish independent geophysical accuracy.</p>
      : <p><b>Output validation:</b> {textValue(validation.status) || 'Not reported for this job.'}</p>}
    {textValue(summary.scientific_limit) && <p>{textValue(summary.scientific_limit)}</p>}
    {warnings.length > 0 && <ul className="science-warnings">{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}

    {diagnostics.length > 0 && <section><h3>Scientific diagnostics</h3><ul className="science-diagnostics">
      {diagnostics.map((diagnostic) => <li key={diagnostic.name}>
        <h4>{diagnostic.name.split('_').join(' ')}</h4>
        <p>{scientificStatusLabel(diagnostic.status)}{diagnostic.status && ` (${diagnostic.status})`}</p>
        {diagnostic.reason && <p>{diagnostic.reason}</p>}
        {diagnostic.observationCount !== undefined && <p>Observations: {diagnostic.observationCount}</p>}
        {diagnostic.validPixels !== undefined && <p>Valid pixels: {diagnostic.validPixels}</p>}
      </li>)}
    </ul></section>}

    {products.length > 0 && <details className="science-products"><summary>Input products ({products.length})</summary>
      <ul>{products.map((product, index) => <li key={index}>
        <strong>{textValue(product.granule_name) || textValue(product.granule) || 'Identity not reported'}</strong>
        <p>{textValue(product.product_type)} · {textValue(product.maturity) || 'Maturity not reported'}</p>
        <p>Reference: {textValue(product.reference_date) || textValue(product.reference) || 'Not reported'} · Secondary: {textValue(product.secondary_date) || textValue(product.secondary) || 'Not reported'}</p>
        {textValue(product.status) && <p>Status: {textValue(product.status)}</p>}
        {textValue(product.reason) && <p>{textValue(product.reason)}</p>}
      </li>)}</ul>
    </details>}

    <details className="science-provenance"><summary>Inputs and processing provenance</summary>
      <dl>
        <dt>Inclusive date window</dt><dd>{job.start_date} to {job.end_date}</dd>
        <dt>Products</dt><dd>{job.product_type || 'BOTH'}</dd>
        <dt>Acquisition selection</dt><dd>{job.acquisition_window === 'both-acquisitions-in-window' ? 'Both acquisitions within the date window' : 'Secondary acquisition within the window; reference may precede it'}</dd>
        <dt>Processing scope</dt><dd>{job.processing_profile || 'full-aoi (legacy job)'}</dd>
        <dt>Retention</dt><dd>{job.retention_mode || 'Not reported for legacy job'}</dd>
        <dt>Science repository</dt><dd>{repository || 'Not reported'}</dd>
        <dt>Science commit</dt><dd>{commit || 'Not reported'}</dd>
        <dt>Pipeline version</dt><dd>{textValue(provenance.pipeline_version) || 'Not reported'}</dd>
        <dt>Actions run</dt><dd>{textValue(provenance.github_run_id) || job.github_run_id || 'Not reported'}</dd>
        <dt>AOI SHA-256</dt><dd>{aoiHash || 'Not reported'}</dd>
        <dt>Submitted AOI (EPSG:4326)</dt><dd><code>{job.aoi_wkt}</code></dd>
      </dl>
    </details>

    {artifacts.length > 0 && <section><h3>Retained scientific outputs</h3><p>Downloads are available to the signed-in job owner. GeoTIFFs, CSVs, JSON and previews appear only when produced and retained.</p>
      <ul className="science-artifacts">{artifacts.map((artifact) => <ArtifactDownload key={artifact.index} artifact={artifact} jobId={job.id} user={user} />)}</ul>
    </section>}
    {legacyUrl && <p><a className="investigate-button validated-output-link" href={legacyUrl} target="_blank" rel="noreferrer">Open retained output</a></p>}
    {!job.result_summary && <p>Scientific results will appear when the processor reports them. Job completion alone does not establish scientific validation.</p>}
  </section>;
}
