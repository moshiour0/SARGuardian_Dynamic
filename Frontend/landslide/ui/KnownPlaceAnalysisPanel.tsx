import { useEffect, useState } from 'react';
import type { User } from 'firebase/auth';
import {
  getKnownPlaceAnalysisStatus,
  startKnownPlaceAnalysis,
  type KnownPlaceAnalysisMode,
  type KnownPlaceAnalysisRun,
  type KnownPlaceHazardMode,
} from '../../src/api/knownPlace';

type StoredRun = KnownPlaceAnalysisRun & { execute_downloads: boolean };

type Props = {
  user: User;
  aoiWkt: string | null;
  startDate: string;
  endDate: string;
  placeName: string | null;
  aoiAreaKm2: number;
  onBack: () => void;
};

const hazardModes: { value: KnownPlaceHazardMode; label: string }[] = [
  { value: 'landslide', label: 'Landslide' },
  { value: 'glacier_collapse', label: 'Glacier collapse' },
  { value: 'ice_rock_avalanche', label: 'Ice–rock avalanche' },
  { value: 'mixed', label: 'Mixed / uncertain' },
];

const analysisModes: { value: KnownPlaceAnalysisMode; label: string }[] = [
  { value: 'exploratory', label: 'Exploratory measurement' },
  { value: 'retrospective_validation', label: 'Retrospective validation' },
];

function storageKey(uid: string): string {
  return `sarguardian.knownPlaceAnalysis.v1:${uid}`;
}

function loadStoredRun(uid: string): StoredRun | null {
  if (typeof window === 'undefined') return null;
  try {
    const value = sessionStorage.getItem(storageKey(uid));
    if (!value) return null;
    const parsed = JSON.parse(value) as Partial<StoredRun>;
    if (typeof parsed.request_id !== 'string' || !/^[0-9a-f-]{36}$/i.test(parsed.request_id)) return null;
    if (typeof parsed.execute_downloads !== 'boolean') return null;
    if (!['dispatching', 'queued', 'in_progress', 'completed'].includes(String(parsed.status))) return null;
    return parsed as StoredRun;
  } catch {
    return null;
  }
}

function resultLabel(run: StoredRun): string {
  if (run.status !== 'completed') return run.status === 'dispatching' ? 'Starting' : 'Running';
  if (run.conclusion === 'success') return 'Workflow completed';
  if (run.conclusion === 'cancelled') return 'Cancelled';
  return 'Workflow failed';
}

export function KnownPlaceAnalysisPanel({ user, aoiWkt, startDate, endDate, placeName, aoiAreaKm2, onBack }: Props) {
  const [hazardMode, setHazardMode] = useState<KnownPlaceHazardMode>('mixed');
  const [analysisMode, setAnalysisMode] = useState<KnownPlaceAnalysisMode>('exploratory');
  const [polarization, setPolarization] = useState<'HH' | 'HV' | 'VV' | 'VH'>('HH');
  const [track, setTrack] = useState('ANY');
  const [direction, setDirection] = useState<'ANY' | 'A' | 'D'>('ANY');
  const [coherenceNeighbours, setCoherenceNeighbours] = useState<'1' | '5' | '10' | '15' | '20' | '25'>('1');
  const [allowedReleaseIds, setAllowedReleaseIds] = useState('');
  const [referenceWkt, setReferenceWkt] = useState('');
  const [geometryHeight, setGeometryHeight] = useState('');
  const [confirmRawDownloads, setConfirmRawDownloads] = useState(false);
  const [pendingAction, setPendingAction] = useState<'dry-run' | 'analysis' | null>(null);
  const [run, setRun] = useState<StoredRun | null>(() => loadStoredRun(user.uid));
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');

  useEffect(() => {
    if (!run) {
      try { sessionStorage.removeItem(storageKey(user.uid)); } catch { /* session storage is optional */ }
      return;
    }
    try { sessionStorage.setItem(storageKey(user.uid), JSON.stringify(run)); } catch { /* session storage is optional */ }
  }, [run, user.uid]);

  useEffect(() => {
    const requestId = run?.request_id;
    if (!requestId || run?.status === 'completed') return undefined;
    let active = true;
    let timeoutId: number | undefined;
    const poll = async () => {
      try {
        const next = await getKnownPlaceAnalysisStatus(requestId, user);
        if (!active) return;
        setPollError('');
        setRun((current) => current && current.request_id === next.request_id
          ? { ...current, ...next, execute_downloads: current.execute_downloads }
          : current);
        if (next.status !== 'completed' && active) timeoutId = window.setTimeout(poll, 3500);
      } catch (statusError) {
        if (!active) return;
        setPollError(statusError instanceof Error ? statusError.message : 'Could not refresh this run yet.');
        timeoutId = window.setTimeout(poll, 8000);
      }
    };
    timeoutId = window.setTimeout(poll, 1200);
    return () => {
      active = false;
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
  }, [run?.request_id, run?.status === 'completed', user]);

  const submit = async (executeDownloads: boolean) => {
    if (!aoiWkt || !startDate || !endDate) {
      setError('Return to the map and create a valid closed AOI and date window first.');
      return;
    }
    if (executeDownloads && !confirmRawDownloads) {
      setError('Confirm raw-data download permission before starting the GOFF measurement run.');
      return;
    }
    if (track.trim().toUpperCase() !== 'ANY' && !/^\d{3}$/.test(track.trim())) {
      setError('Track must be ANY or an exact three-digit value such as 098.');
      return;
    }
    const height = geometryHeight.trim() ? Number(geometryHeight) : null;
    if (height !== null && (!Number.isFinite(height) || height < -500 || height > 100000)) {
      setError('Enter a finite ellipsoidal height between −500 and 100,000 metres, or leave it blank.');
      return;
    }
    setPendingAction(executeDownloads ? 'analysis' : 'dry-run');
    setError('');
    setPollError('');
    try {
      const submitted = await startKnownPlaceAnalysis({
        aoi_wkt: aoiWkt,
        start_date: startDate,
        end_date: endDate,
        hazard_mode: hazardMode,
        analysis_mode: analysisMode,
        polarization,
        track: track.trim().toUpperCase(),
        direction,
        coherence_neighbours: coherenceNeighbours,
        allowed_release_ids: allowedReleaseIds.trim(),
        reference_wkt: referenceWkt.trim(),
        geometry_height_m: height,
        execute_downloads: executeDownloads,
        confirm_raw_downloads: executeDownloads && confirmRawDownloads,
      }, user);
      setRun({ ...submitted, execute_downloads: executeDownloads });
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : 'Could not submit Known Place Analysis.');
    } finally {
      setPendingAction(null);
    }
  };

  const runTerminal = run?.status === 'completed';
  const runSucceeded = runTerminal && run?.conclusion === 'success';
  const runFailed = runTerminal && !runSucceeded;

  return (
    <main className="nisar-results-main displacement-preparation known-place-analysis">
      <section className="nisar-results-summary">
        <span className="result-kicker">SEPARATE SCIENCE WORKFLOW · GOFF</span>
        <h1>Known Place Analysis</h1>
        <p>Run the dedicated GOFF analysis workflow for the AOI and dates selected on the map. This is separate from Find Displacements, which continues to use the GUNW + GOFF account-processing pipeline.</p>
        <div className="catalog-stats">
          <div><span>Known place</span><strong>{placeName || 'AOI only — no named place'}</strong></div>
          <div><span>AOI area</span><strong>{Number.isFinite(aoiAreaKm2) && aoiAreaKm2 > 0 ? `${aoiAreaKm2.toFixed(2)} km²` : 'Not validated'}</strong></div>
          <div><span>Start date</span><strong>{startDate || 'Not selected'}</strong></div>
          <div><span>End date</span><strong>{endDate || 'Not selected'}</strong></div>
        </div>
        <p className="catalog-notice">The polygon is the analysis region. A selected place name is context metadata, not proof that a hazard exists. The engine reports horizontal-offset measurements, uncertainty and quality gates where supported; it does not create a hazard alarm.</p>
        {!aoiWkt && <p className="catalog-error" role="alert">A valid closed AOI is required. Go back to the map and draw your polygon.</p>}
        <div className="known-place-form-grid">
          <label className="aoi-job-label">
            <span>Hazard context</span>
            <select value={hazardMode} onChange={(event) => setHazardMode(event.target.value as KnownPlaceHazardMode)}>
              {hazardModes.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label className="aoi-job-label">
            <span>Analysis mode</span>
            <select value={analysisMode} onChange={(event) => setAnalysisMode(event.target.value as KnownPlaceAnalysisMode)}>
              {analysisModes.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label className="aoi-job-label">
            <span>Polarization</span>
            <select value={polarization} onChange={(event) => setPolarization(event.target.value as 'HH' | 'HV' | 'VV' | 'VH')}>
              {(['HH', 'HV', 'VV', 'VH'] as const).map((value) => <option key={value}>{value}</option>)}
            </select>
          </label>
          <label className="aoi-job-label">
            <span>Track</span>
            <input value={track} onChange={(event) => setTrack(event.target.value)} maxLength={3} placeholder="ANY or 098" autoComplete="off" />
          </label>
          <label className="aoi-job-label">
            <span>Flight direction</span>
            <select value={direction} onChange={(event) => setDirection(event.target.value as 'ANY' | 'A' | 'D')}>
              <option value="ANY">Any — keep orbit chains separate</option>
              <option value="A">Ascending</option>
              <option value="D">Descending</option>
            </select>
          </label>
          <label className="aoi-job-label">
            <span>Minimum valid coherence neighbours</span>
            <select value={coherenceNeighbours} onChange={(event) => setCoherenceNeighbours(event.target.value as typeof coherenceNeighbours)}>
              {(['1', '5', '10', '15', '20', '25'] as const).map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <label className="aoi-job-label known-place-wide">
            <span>Exact release IDs (optional, comma-separated)</span>
            <input value={allowedReleaseIds} onChange={(event) => setAllowedReleaseIds(event.target.value)} placeholder="Leave blank to retain valid candidates; conflicts fail safe" autoComplete="off" />
          </label>
          <label className="aoi-job-label known-place-wide">
            <span>Reference/background polygon WKT (optional)</span>
            <textarea value={referenceWkt} onChange={(event) => setReferenceWkt(event.target.value)} rows={3} placeholder="Leave blank for the documented 2 km local-context component-median reference" spellCheck={false} />
          </label>
          <label className="aoi-job-label">
            <span>Constant ellipsoidal height in metres (optional sensitivity only)</span>
            <input type="number" value={geometryHeight} onChange={(event) => setGeometryHeight(event.target.value)} min={-500} max={100000} step="any" placeholder="Blank = documented approximation" />
          </label>
        </div>

        <section className="known-place-run-options" aria-label="Known Place run options">
          <h2>Choose how to run</h2>
          <p className="catalog-notice"><b>Catalog-only dry run:</b> checks the request, product availability and series plan. It does not download raw NISAR products.</p>
          <button type="button" className="demo-button" disabled={!aoiWkt || pendingAction !== null} onClick={() => void submit(false)}>
            {pendingAction === 'dry-run' ? 'Submitting dry run…' : 'Run catalog-only dry run'}
          </button>
          <div className="known-place-consent">
            <label>
              <input type="checkbox" checked={confirmRawDownloads} onChange={(event) => setConfirmRawDownloads(event.target.checked)} />
              <span><b>I explicitly authorize raw NISAR GOFF downloads for this request.</b> The workflow checks Earthdata/Drive credentials before downloading, processes selected products one at a time, and retains derived results and receipts.</span>
            </label>
            <button type="button" className="investigate-button" disabled={!aoiWkt || !confirmRawDownloads || pendingAction !== null} onClick={() => void submit(true)}>
              {pendingAction === 'analysis' ? 'Starting GOFF analysis…' : 'Run Known Place Analysis'}
            </button>
          </div>
          {error && <p className="catalog-error" role="alert">{error}</p>}
        </section>

        {run && (
          <section className="known-place-run-status" aria-live="polite">
            <div className="known-place-run-heading">
              <span className="result-kicker">RUN STATUS</span>
              <strong>{resultLabel(run)}</strong>
            </div>
            <p><b>Request ID:</b> <code>{run.request_id}</code></p>
            {run.github_run_id && <p><b>GitHub Actions run:</b> <code>{run.github_run_id}</code></p>}
            <p><b>Mode:</b> {run.execute_downloads ? 'GOFF measurement with raw-data download opt-in' : 'Catalog-only dry run — no raw download'}</p>
            {run.conclusion && <p><b>Conclusion:</b> {run.conclusion}</p>}
            {run.notice && <p className="catalog-notice">{run.notice}</p>}
            {pollError && !runTerminal && <p className="action-hint" role="status">{pollError} Automatic status checks will retry.</p>}
            {run.github_run_url && <a className="known-place-run-link" href={run.github_run_url} target="_blank" rel="noreferrer">Open GitHub run and view the result bundle</a>}
            {runSucceeded && <p className="catalog-notice">Workflow succeeded. Inspect the measurement, uncertainty and reference-method reports in the run artifact; a completed run is not independent hazard-detection validation.</p>}
            {runFailed && <p className="catalog-error">Workflow ended with {run.conclusion || 'an error'}. Open the run link above to see the failing stage and safe error summary.</p>}
          </section>
        )}
        <button type="button" className="workspace-nav-link known-place-back" onClick={onBack}>← Back to AOI map</button>
      </section>
    </main>
  );
}
