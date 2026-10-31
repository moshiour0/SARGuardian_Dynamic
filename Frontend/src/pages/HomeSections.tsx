import { useEffect, useState } from 'react';
import type { User } from 'firebase/auth';
import { listDisplacementJobs, type DisplacementJob } from '../api/catalog';
import { ScientificResults } from '../../landslide/ui/ScientificResults';

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? 'Date not available'
    : date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function ArchiveSection({ user, onSignIn, onOpenWorkspace }: {
  user: User | null;
  onSignIn: () => void;
  onOpenWorkspace: () => void;
}) {
  const [jobs, setJobs] = useState<DisplacementJob[]>([]);
  const [state, setState] = useState<'signed-out' | 'loading' | 'ready' | 'error'>(
    user ? 'loading' : 'signed-out',
  );
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    if (!user) {
      setJobs([]);
      setState('signed-out');
      setError('');
      return () => { active = false; };
    }

    setState('loading');
    setError('');
    void listDisplacementJobs(user)
      .then((items) => {
        if (!active) return;
        setJobs([...items].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)));
        setState('ready');
      })
      .catch((requestError: unknown) => {
        if (!active) return;
        setError(requestError instanceof Error ? requestError.message : 'The account archive could not be loaded.');
        setState('error');
      });

    return () => { active = false; };
  }, [user]);

  return (
    <section className="home-section archive-section" id="archive" aria-labelledby="archive-title">
      <div className="home-section-heading">
        <div>
          <p className="section-eyebrow">YOUR WORKSPACE</p>
          <h2 id="archive-title">Analysis archive</h2>
          <p>Previous tasks are loaded from your signed-in account. No sample jobs are inserted into this list.</p>
        </div>
        {user && <button className="text-link-button" type="button" onClick={onOpenWorkspace}>Open analysis workspace</button>}
      </div>

      {state === 'signed-out' && (
        <div className="archive-empty">
          <strong>Sign in to view your analysis history.</strong>
          <p>Only jobs belonging to your account are shown here.</p>
          <button className="section-action" type="button" onClick={onSignIn}>Sign in</button>
        </div>
      )}
      {state === 'loading' && <p className="section-status" role="status">Loading account analysis history…</p>}
      {state === 'error' && (
        <div className="archive-empty" role="alert">
          <strong>Archive unavailable</strong>
          <p>{error}</p>
        </div>
      )}
      {state === 'ready' && jobs.length === 0 && (
        <div className="archive-empty">
          <strong>No analysis jobs in this account yet.</strong>
          <p>When you submit a processing task, its current status and verified outputs will appear here.</p>
          <button className="section-action" type="button" onClick={onOpenWorkspace}>Start an analysis</button>
        </div>
      )}
      {state === 'ready' && jobs.length > 0 && (
        <div className="archive-list">
          {jobs.slice(0, 5).map((job) => (
            <article className="archive-item" key={job.id}>
              <div className="archive-item-main">
                <div className="archive-item-topline">
                  <span className={`archive-state archive-state-${job.state}`}>{job.state.replaceAll('_', ' ')}</span>
                  <span className="archive-job-id">Job {job.id.slice(0, 8)}</span>
                </div>
                <h3>{job.place_name || 'AOI-wide displacement analysis'}</h3>
                <p>{formatDate(job.start_date)} – {formatDate(job.end_date)}</p>
                <small>Updated {formatDate(job.updated_at)} · {job.stage}</small>
              </div>
              <div className="archive-item-actions">
                {['queued', 'dispatching', 'running'].includes(job.state) && (
                  <button className="section-action secondary" type="button" onClick={onOpenWorkspace}>Open processing</button>
                )}
                <ScientificResults job={job} user={user!} />
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

export function HomeSections({ user, onSignIn, onOpenWorkspace }: {
  user: User | null;
  onSignIn: () => void;
  onOpenWorkspace: () => void;
}) {
  return (
    <>
      <section className="home-section how-section" id="how-it-works" aria-labelledby="how-title">
        <div className="home-section-heading">
          <div>
            <p className="section-eyebrow">FROM OBSERVATION TO EVIDENCE</p>
            <h2 id="how-title">How SARGuardian works</h2>
            <p>Start with an area and a time window. The system checks what the available radar observations can support.</p>
          </div>
        </div>
        <div className="method-steps">
          <article className="method-step">
            <span className="step-number">01</span>
            <h3>Choose an area</h3>
            <p>Search for a place or draw an AOI polygon on the map, then choose the observation dates.</p>
          </article>
          <article className="method-step">
            <span className="step-number">02</span>
            <h3>Discover observations</h3>
            <p>Check the NISAR archive for matching GUNW and GOFF products before a processing job is submitted.</p>
          </article>
          <article className="method-step">
            <span className="step-number">03</span>
            <h3>Process and screen</h3>
            <p>Run the AOI workflow, retain quality and geometry checks, and search the area for candidate patterns.</p>
          </article>
          <article className="method-step">
            <span className="step-number">04</span>
            <h3>Review the evidence</h3>
            <p>Explore maps, observations, supported measurements, limitations and data provenance.</p>
          </article>
        </div>
      </section>

      <section className="home-section cases-section" id="cases" aria-labelledby="cases-title">
        <div className="cases-copy">
          <p className="section-eyebrow">RESEARCH RECORDS</p>
          <h2 id="cases-title">Cases built on traceable evidence</h2>
          <p>Public case studies should be published only with a reproducible result bundle, source-data references and clear validation notes. This section will list those records when they are ready.</p>
          <p className="section-note">No demo detections, invented measurements or placeholder validation scores are presented as research results.</p>
        </div>
        <div className="cases-reference">
          <span className="reference-label">RESEARCH WORKFLOW</span>
          <strong>Known Place Analysis</strong>
          <p>Revisit a defined location with an explicit target and inspect its measurement support independently from AOI-wide discovery.</p>
          <button className="section-action" type="button" onClick={onOpenWorkspace}>Open analysis workspace</button>
        </div>
      </section>

      <section className="home-section method-section" id="method" aria-labelledby="method-title">
        <div className="home-section-heading">
          <div>
            <p className="section-eyebrow">METHOD</p>
            <h2 id="method-title">Evidence before interpretation</h2>
            <p>Each processing output is presented with its measurement scope and quality limits.</p>
          </div>
        </div>
        <div className="method-guardrails">
          <article>
            <h3>Candidate is not confirmation</h3>
            <p>A spatial anomaly is a review lead. It is not, by itself, measured ground movement or a confirmed hazard.</p>
          </article>
          <article>
            <h3>LOS is not 3D motion</h3>
            <p>Pairwise line-of-sight measurements do not automatically represent cumulative, vertical or three-dimensional displacement.</p>
          </article>
          <article>
            <h3>Withheld is not zero</h3>
            <p>When coverage, geometry or quality gates fail, the measurement is reported as unavailable or withheld—not as zero.</p>
          </article>
        </div>
      </section>

      <section className="home-section data-section" id="data" aria-labelledby="data-title">
        <div className="home-section-heading">
          <div>
            <p className="section-eyebrow">DATA AND SOURCES</p>
            <h2 id="data-title">Working with NISAR observations</h2>
            <p>The products answer different questions and are shown separately in the analysis workspace.</p>
          </div>
        </div>
        <div className="data-cards">
          <article className="data-card">
            <span className="data-code">L2 · GUNW</span>
            <h3>Unwrapped interferograms</h3>
            <p>Provide pairwise line-of-sight phase-derived displacement observations where valid pixels and compatible acquisition networks support measurement.</p>
          </article>
          <article className="data-card">
            <span className="data-code">L2 · GOFF</span>
            <h3>Offset tracking</h3>
            <p>Provides image-space offset evidence for screening. An offset anomaly is not automatically a geophysical displacement measurement.</p>
          </article>
          <article className="data-card">
            <span className="data-code">ARCHIVE</span>
            <h3>ASF catalogue</h3>
            <p>Catalogue discovery identifies products matching the AOI and date window. A catalogue preflight is not a download or scientific analysis.</p>
          </article>
        </div>
      </section>

      <ArchiveSection user={user} onSignIn={onSignIn} onOpenWorkspace={onOpenWorkspace} />
    </>
  );
}
