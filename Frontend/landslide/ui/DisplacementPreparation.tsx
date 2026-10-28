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
  return <section className="displacement-job-status" aria-live="polite">
    <h2>Processing status</h2>
    <p><b>Status:</b> {job.state}</p><p><b>Job ID:</b> {job.id}</p>
    <p><b>Stage:</b> {job.stage}</p>
    <div className="displacement-place-context" aria-label="Selected place and measurement target">
      <p><b>Selected place:</b> {job.place_name || 'No named place selected'}</p>
      {Number.isFinite(job.place_latitude) && Number.isFinite(job.place_longitude)
        && <p><b>Place coordinates:</b> {job.place_latitude!.toFixed(5)}, {job.place_longitude!.toFixed(5)}
          {job.target_latitude == null || job.target_longitude == null ? ' (context marker only; outside AOI or not selected as an in-AOI target)' : ''}</p>}
      {Number.isFinite(job.target_latitude) && Number.isFinite(job.target_longitude)
        ? <p><b>Measurement target:</b> {job.target_latitude!.toFixed(5)}, {job.target_longitude!.toFixed(5)}
          <small style={{ display: 'block' }}>In-AOI pair sampling point only; not a stable-ground reference and not a hazard finding.</small></p>
        : <p><b>Measurement target:</b> Not supplied inside the submitted AOI. Target-local metrics and common-reference inversion remain withheld.</p>}
    </div>
    <p><b>Submitted:</b> {new Date(job.created_at).toLocaleString()}</p>
    <p><b>Age:</b> <time data-testid="job-age">{formatElapsed(job.created_at, job.updated_at, job.state, clock)}</time></p>
    {counts && <div className="catalog-stats displacement-granule-counts" aria-label="Granule counts">
      <div><span>GUNW granules</span><strong>{counts.GUNW}</strong></div>
      <div><span>GOFF granules</span><strong>{counts.GOFF}</strong></div>
      <div><span>Total granules</span><strong>{counts.GUNW + counts.GOFF}</strong></div>
      <div><span>Count source</span><strong>{counts.source === 'processed-results' ? 'Processed results' : counts.source === 'worker-discovery' ? 'Worker discovery' : 'ASF catalogue preflight'}</strong></div>
    </div>}
    {job.error_message && <p role="alert">{job.error_message}</p>}
    {hasActiveJob && <div className="displacement-task-actions">
      <button type="button" className="demo-button danger-button" disabled={busy} onClick={onCancel}>{pendingAction === 'cancel' ? 'Cancelling…' : 'Cancel task'}</button>
      <button type="button" className="investigate-button" disabled={busy} onClick={onRefresh}>{pendingAction === 'refresh' ? 'Refreshing…' : 'Refresh task'}</button>
    </div>}
    {job.state === 'failed' && <div className="displacement-task-actions">
      <button type="button" className="investigate-button" disabled={busy} onClick={onRetry}>{pendingAction === 'retry' ? 'Retrying…' : 'Retry task'}</button>
      <button type="button" className="demo-button" disabled={busy} onClick={onBack}>Go to map</button>
    </div>}
    <ScientificResults job={job} user={user} />
  </section>;
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
