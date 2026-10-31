import { useCallback, useEffect, useRef, useState } from 'react';
import type { User } from 'firebase/auth';
import type { PreparedInvestigation } from '../engine/investigation';
import {
  cancelDisplacementJob,
  getDisplacementJob,
  listDisplacementJobs,
  resubmitDisplacementJob,
  DEFAULT_DISPLACEMENT_OPTIONS,
  type DisplacementProcessingOptions,
  type DisplacementJob,
} from '../../src/api/catalog';
import { ScientificResults } from './ScientificResults';
import { chooseAccountJob } from '../engine/displacementJobSelection';
import { ELAPSED_TICK_MS, formatElapsed, type GranuleCounts } from './processingStatus';

const activeStates = new Set(['queued', 'dispatching', 'running']);
const noopTaskActivityChange = () => {};
export const JOB_STATUS_POLL_MS = 2000;

function resultGranuleCounts(job: DisplacementJob): GranuleCounts | null {
  const products = job.result_summary?.products;
  if (Array.isArray(products)) return {
      GUNW: products.filter((product) => product && typeof product === 'object' && (product as Record<string, unknown>).product_type === 'GUNW').length,
      GOFF: products.filter((product) => product && typeof product === 'object' && (product as Record<string, unknown>).product_type === 'GOFF').length,
      source: 'processed-results',
    };
  const discovered = job.result_summary?.catalog_granule_counts;
  if (discovered && typeof discovered === 'object') {
    const counts = discovered as Record<string, unknown>;
    if (Number.isInteger(counts.GUNW) && Number.isInteger(counts.GOFF)) {
      return { GUNW: counts.GUNW as number, GOFF: counts.GOFF as number, source: 'worker-discovery' };
    }
  }
  return null;
}

type PendingAction = 'cancel' | 'refresh' | 'retry' | null;

function jobStateDescription(state: DisplacementJob['state']): string {
  switch (state) {
    case 'queued': return 'The job is queued and waiting for a worker.';
    case 'dispatching': return 'The job is being handed to the processing workflow.';
    case 'running': return 'The processing workflow is running. Status is refreshed from the server.';
    case 'succeeded': return 'The processing job has ended. Review only the outputs that passed their validation checks.';
    case 'failed': return 'The processing workflow stopped before successful completion.';
    case 'cancelled': return 'The processing job was cancelled.';
  }
}

function safeWorkflowUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'github.com' ? url.toString() : null;
  } catch {
    return null;
  }
}

export function DisplacementJobStatus({ job, pendingAction, clock, granuleCounts, onCancel, onRefresh, onRetry, onBack, user }: {
  job: DisplacementJob;
  user: Pick<User, 'getIdToken'>;
  pendingAction: PendingAction;
  clock: number;
  granuleCounts: GranuleCounts | null;
  onCancel: () => void;
  onRefresh: () => void;
  onRetry: () => void;
  onBack: () => void;
}) {
  const hasActiveJob = activeStates.has(job.state);
  const counts = resultGranuleCounts(job) || granuleCounts;
  const busy = pendingAction !== null;
  const workflowUrl = safeWorkflowUrl(job.github_run_url);
  const dateWindow = `${new Date(job.start_date + 'T00:00:00').toLocaleDateString()} – ${new Date(job.end_date + 'T00:00:00').toLocaleDateString()}`;
  const hasTarget = Number.isFinite(job.target_latitude) && Number.isFinite(job.target_longitude);

  return (
    <section className="displacement-job-status processing-v2" aria-live="polite">
      <div className="processing-v2-heading">
        <div>
          <span className="result-kicker">LIVE JOB STATUS</span>
          <h2>{job.state === 'succeeded' ? 'Processing finished' : job.state === 'failed' ? 'Processing needs attention' : job.state === 'cancelled' ? 'Processing cancelled' : 'Processing in progress'}</h2>
          <p>{jobStateDescription(job.state)}</p>
        </div>
        <span className={`processing-state-pill processing-state-${job.state}`}>{job.state}</span>
      </div>

      <div className="processing-summary-grid">
        <article><span>AOI</span><strong>Submitted polygon</strong><small>{job.place_name || 'No named place attached'}</small></article>
        <article><span>Date window</span><strong>{dateWindow}</strong><small>Requested observation period</small></article>
        <article><span>Analysis scope</span><strong>AOI-wide screening</strong><small>{hasTarget ? 'A target coordinate was also supplied' : 'No measurement target required'}</small></article>
        <article><span>Job reference</span><strong className="processing-job-id">{job.id}</strong><small>Submitted {new Date(job.created_at).toLocaleString()}</small></article>
      </div>

      <div className="processing-detail-grid">
        <section className="processing-detail-panel" aria-labelledby="processing-stage-title">
          <div className="processing-panel-heading">
            <div><span className="result-kicker">WORKFLOW STATUS</span><h3 id="processing-stage-title">Current processing stage</h3></div>
            <span className="processing-stage-mark">{job.state === 'running' ? 'Active' : job.state}</span>
          </div>
          <p className="processing-stage-name">{job.stage || 'Stage not reported'}</p>
          <p className="processing-stage-note">Elapsed task age: <strong><time data-testid="job-age">{formatElapsed(job.created_at, job.updated_at, job.state, clock)}</time></strong></p>
          <div className="processing-products">
            <div className="processing-panel-heading"><h4>Product inventory</h4><span>Source: {counts ? counts.source === 'processed-results' ? 'Processed results' : counts.source === 'worker-discovery' ? 'Worker discovery' : 'ASF catalogue preflight' : 'Not reported'}</span></div>
            {counts ? (
              <div className="processing-product-grid">
                <article><span>GUNW</span><strong>{counts.GUNW}</strong></article>
                <article><span>GOFF</span><strong>{counts.GOFF}</strong></article>
                <article><span>Total</span><strong>{counts.GUNW + counts.GOFF}</strong></article>
              </div>
            ) : <p className="processing-not-reported">Product counts are not included in the current job response.</p>}
          </div>
          {workflowUrl && <a className="processing-workflow-link" href={workflowUrl} target="_blank" rel="noreferrer">Open workflow run details <span aria-hidden="true">→</span></a>}
        </section>

        <section className="processing-detail-panel baseline-readiness-panel" aria-labelledby="baseline-readiness-title">
          <div className="processing-panel-heading">
            <div><span className="result-kicker">INTERFEROMETRIC GEOMETRY</span><h3 id="baseline-readiness-title">Baseline analysis</h3></div>
            <span className="processing-stage-mark">Metadata pending</span>
          </div>
          <p>Baseline geometry helps assess acquisition-pair suitability. It is not a displacement measurement.</p>
          <div className="baseline-readiness-grid">
            <article><span>Temporal baseline</span><strong>Date-derived</strong><small>Requires pair-level reference and secondary dates in the result manifest.</small></article>
            <article><span>Perpendicular baseline · B⊥</span><strong>Not exposed</strong><small>Not currently published by the frontend job-result contract.</small></article>
            <article><span>Parallel baseline · B∥</span><strong>Not exposed</strong><small>Not currently published by the frontend job-result contract.</small></article>
          </div>
          <div className="baseline-empty-state">
            <strong>Baseline plot not available for this job yet.</strong>
            <p>No scatter points, ranges or zero values are drawn until real pair metadata, units and AOI coverage have been extracted and validated.</p>
          </div>
        </section>
      </div>

      <div className="processing-science-note">
        A processing PASS confirms structural and provenance checks; it does not independently validate ground movement. Pairwise LOS is not automatically cumulative or 3-D motion. A withheld measurement is not zero.
      </div>

      {job.error_message && <p className="processing-job-error" role="alert">{job.error_message}</p>}

      <div className="displacement-task-actions processing-actions">
        {hasActiveJob && <>
          <button type="button" className="demo-button danger-button" disabled={busy} onClick={onCancel}>{pendingAction === 'cancel' ? 'Cancelling…' : 'Cancel task'}</button>
          <button type="button" className="investigate-button" disabled={busy} onClick={onRefresh}>{pendingAction === 'refresh' ? 'Refreshing…' : 'Refresh status'}</button>
        </>}
        {job.state === 'failed' && <>
          <button type="button" className="investigate-button" disabled={busy} onClick={onRetry}>{pendingAction === 'retry' ? 'Retrying…' : 'Retry failed task'}</button>
          <button type="button" className="demo-button" onClick={onBack}>Return to map</button>
        </>}
        {job.state === 'cancelled' && <button type="button" className="demo-button" onClick={onBack}>Return to map</button>}
      </div>

      <ScientificResults job={job} user={user} />
    </section>
  );
}

export function DisplacementPreparation({ preparation, options = DEFAULT_DISPLACEMENT_OPTIONS, granuleCounts = null, onBack, onTaskActivityChange = noopTaskActivityChange, user }: { preparation: PreparedInvestigation; options?: DisplacementProcessingOptions; granuleCounts?: GranuleCounts | null; onBack: () => void; onTaskActivityChange?: (active: boolean) => void; user: User }) {
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const [job, setJob] = useState<DisplacementJob | null>(null);
  const [jobError, setJobError] = useState('');
  const [accountLoaded, setAccountLoaded] = useState(false);
  const [clock, setClock] = useState(() => Date.now());
  const statusRequestSequence = useRef(0);
  const userActionActive = useRef(false);
  const jobId = job?.id;
  const hasActiveJob = Boolean(job && activeStates.has(job.state));

  // This route performs one account-job lookup when it mounts. The parent
  // workspace can rerender when its global active-task flag changes; its
  // preparation object is reconstructed on each render. Closing over that
  // object here would restart the account lookup just as a job completes.
  // In deployments whose list endpoint omits completed jobs, that second
  // lookup could clear the successful job and trigger the empty-route redirect.
  // Keep the latest inputs in a ref, but keep the loader identity stable.
  const accountLookupContext = useRef({ preparation, options, user });
  accountLookupContext.current = { preparation, options, user };

  const loadAccountJob = useCallback(async () => {
    setJobError('');
    const lookup = accountLookupContext.current;
    try {
      const jobs = await listDisplacementJobs(lookup.user);
      setJob((current) => current && activeStates.has(current.state)
        ? current
        : chooseAccountJob(jobs, lookup.preparation, lookup.options));
    } catch (error) {
      setJobError(error instanceof Error ? error.message : 'Could not load your displacement jobs.');
    } finally {
      setAccountLoaded(true);
    }
  }, []);

  // Browser refresh and reopening this route read durable account state once;
  // status polling below then follows the selected job by its stable ID.
  useEffect(() => { void loadAccountJob(); }, [loadAccountJob]);

  useEffect(() => {
    if (!jobId || !hasActiveJob) return undefined;
    let stopped = false;
    let requestInFlight = false;
    const refresh = () => {
      if (stopped || requestInFlight || userActionActive.current) return;
      requestInFlight = true;
      const requestSequence = ++statusRequestSequence.current;
      void getDisplacementJob(jobId, user)
        .then((nextJob) => {
          if (stopped || requestSequence !== statusRequestSequence.current) return;
          setJob(nextJob);
          setJobError('');
        })
        .catch((error: unknown) => {
          if (!stopped && requestSequence === statusRequestSequence.current) {
            setJobError(error instanceof Error ? error.message : 'Could not refresh the displacement job.');
          }
        })
        .finally(() => { requestInFlight = false; });
    };
    refresh();
    const timer = window.setInterval(() => {
      refresh();
    }, JOB_STATUS_POLL_MS);
    return () => {
      stopped = true;
      statusRequestSequence.current += 1;
      window.clearInterval(timer);
    };
  }, [hasActiveJob, jobId, user]);

  // The displayed elapsed time is local UI state, not a server poll. Keep it
  // smooth without turning the account-status API into a once-per-second poll.
  useEffect(() => {
    if (!hasActiveJob) return undefined;
    setClock(Date.now());
    const timer = window.setInterval(() => setClock(Date.now()), ELAPSED_TICK_MS);
    return () => window.clearInterval(timer);
  }, [hasActiveJob]);

  useEffect(() => {
    if (job) onTaskActivityChange(activeStates.has(job.state));
  }, [job, onTaskActivityChange]);

  // This route is only for an account task already created on the map. It is
  // never a second submission screen, and a direct/expired route returns to
  // the map once the account lookup has completed.
  useEffect(() => {
    if (accountLoaded && !job && !jobError) onBack();
  }, [accountLoaded, job, jobError, onBack]);

  const cancel = useCallback(async () => {
    if (!job || !activeStates.has(job.state) || pendingAction || userActionActive.current) return;
    userActionActive.current = true;
    statusRequestSequence.current += 1;
    setPendingAction('cancel');
    setJobError('');
    try {
      const cancelled = await cancelDisplacementJob(job.id, user);
      setJob(cancelled);
      if (cancelled.state === 'cancelled' && cancelled.stage === 'cancelled_by_user') {
        onTaskActivityChange(false);
        onBack();
      }
    } catch (error) {
      setJobError(error instanceof Error ? error.message : 'Could not cancel the displacement job.');
    } finally {
      userActionActive.current = false;
      setPendingAction(null);
    }
  }, [job, onBack, onTaskActivityChange, pendingAction, user]);

  const refreshManually = useCallback(async () => {
    if (!job || pendingAction || userActionActive.current) return;
    const requestedJobId = job.id;
    userActionActive.current = true;
    const requestSequence = ++statusRequestSequence.current;
    setPendingAction('refresh');
    setJobError('');
    try {
      const nextJob = await getDisplacementJob(requestedJobId, user);
      if (requestSequence === statusRequestSequence.current) {
        setJob((current) => current?.id === requestedJobId ? nextJob : current);
        setClock(Date.now());
      }
    } catch (error) {
      if (requestSequence === statusRequestSequence.current) {
        setJobError(error instanceof Error ? error.message : 'Could not refresh the displacement job.');
      }
    } finally {
      userActionActive.current = false;
      setPendingAction(null);
    }
  }, [job, pendingAction, user]);

  const retryFailed = useCallback(async () => {
    if (!job || job.state !== 'failed' || pendingAction || userActionActive.current) return;
    userActionActive.current = true;
    statusRequestSequence.current += 1;
    setPendingAction('retry');
    setJobError('');
    try {
      const replacement = await resubmitDisplacementJob(job.id, crypto.randomUUID(), user);
      setJob(replacement);
      onTaskActivityChange(true);
    } catch (error) {
      setJobError(error instanceof Error ? error.message : 'Could not retry the displacement job.');
    } finally {
      userActionActive.current = false;
      setPendingAction(null);
    }
  }, [job, onTaskActivityChange, pendingAction, user]);

  if (!job && !jobError) return null;

  return <main className="nisar-results-main displacement-preparation">
    <section className="nisar-results-summary">
      <span className="result-kicker">DISPLACEMENT PROCESSING</span>
      <h1>Find Displacements</h1>
      <p>Your processing task belongs to this signed-in account and continues if you close the browser. Reopen this page to see its current state.</p>
      {job && <DisplacementJobStatus job={job} pendingAction={pendingAction} clock={clock} granuleCounts={granuleCounts} user={user}
        onCancel={() => void cancel()} onRefresh={() => void refreshManually()}
        onRetry={() => void retryFailed()} onBack={onBack} />}
      {jobError && <><p role="alert">{jobError}</p>{!job && <button type="button" className="demo-button" onClick={onBack}>Go to map</button>}</>}
    </section>
  </main>;
}
