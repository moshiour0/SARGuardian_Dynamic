import { useState } from 'react';
import type { User } from 'firebase/auth';
import type { DisplacementJob } from '../../src/api/catalog';
import { getInteractiveDashboardHtml } from '../../src/api/catalog';

function driveFolderUrl(job: DisplacementJob): string | undefined {
  if (job.state !== 'succeeded' || !job.result_summary) return undefined;
  const storage = job.result_summary.storage_verification;
  if (!storage || typeof storage !== 'object' || Array.isArray(storage)) return undefined;
  const record = storage as Record<string, unknown>;
  const folderId = record.remote_folder_id;
  if (record.status !== 'PASS' || typeof folderId !== 'string'
      || !/^[A-Za-z0-9_-]{10,200}$/.test(folderId)) return undefined;
  return `https://drive.google.com/drive/folders/${encodeURIComponent(folderId)}`;
}

function dashboardAvailable(job: DisplacementJob): boolean {
  if (job.state !== 'succeeded' || !job.result_summary) return false;
  const dashboard = job.result_summary.web_dashboard;
  if (!dashboard || typeof dashboard !== 'object' || Array.isArray(dashboard)) return false;
  const record = dashboard as Record<string, unknown>;
  if (record.status !== 'PASS') return false;
  const artifacts = job.result_summary.artifacts;
  return Array.isArray(artifacts) && artifacts.some((item) =>
    !!item && typeof item === 'object' && !Array.isArray(item)
      && (item as Record<string, unknown>).path === 'aoi-derived-results.zip',
  );
}

export function ScientificResults({ job, user }: { job: DisplacementJob; user: Pick<User, 'getIdToken'> }) {
  const folderUrl = driveFolderUrl(job);
  const canOpenDashboard = dashboardAvailable(job);
  const [opening, setOpening] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [dashboardHtml, setDashboardHtml] = useState<string | null>(null);
  const [dashboardError, setDashboardError] = useState('');

  async function loadDashboard() {
    if (dashboardHtml) {
      setExpanded((current) => !current);
      return;
    }
    setDashboardError('');
    setOpening(true);
    try {
      const html = await getInteractiveDashboardHtml(job.id, user);
      setDashboardHtml(html);
      setExpanded(true);
    } catch (error: unknown) {
      setDashboardError(error instanceof Error ? error.message : 'Could not load the interactive dashboard.');
    } finally {
      setOpening(false);
    }
  }

  if (!folderUrl && !canOpenDashboard) return null;
  return (
    <section className="scientific-results" aria-label="Scientific result outputs">
      <div className="scientific-results-actions">
        {canOpenDashboard && (
          <button
            className="investigate-button validated-output-link"
            type="button"
            onClick={() => { void loadDashboard(); }}
            disabled={opening}
            aria-expanded={expanded}
          >
            {opening ? 'Loading dashboard…' : expanded ? 'Hide dashboard' : dashboardHtml ? 'Show dashboard' : 'Load dashboard in Processing'}
          </button>
        )}
        {folderUrl && (
          <a className="investigate-button validated-output-link" href={folderUrl} target="_blank" rel="noreferrer">
            Download results
          </a>
        )}
      </div>
      {canOpenDashboard && <p className="scientific-results-note">The dashboard is embedded here in Processing. Google Maps and georeferenced science layers use the verified result bundle. Processing PASS is not independent geophysical validation.</p>}
      {dashboardError && <p className="scientific-results-error" role="alert">{dashboardError}</p>}
      {expanded && dashboardHtml && (
        <div className="scientific-dashboard-frame" aria-label="Interactive scientific dashboard">
          <iframe
            title="SARGuardian interactive scientific dashboard"
            srcDoc={dashboardHtml}
            referrerPolicy="origin"
            sandbox="allow-scripts allow-forms allow-popups"
            allow="fullscreen"
            loading="lazy"
          />
        </div>
      )}
    </section>
  );
}
