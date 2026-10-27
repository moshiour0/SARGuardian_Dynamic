import { useState } from 'react';
import type { User } from 'firebase/auth';
import type { DisplacementJob } from '../../src/api/catalog';
import { getInteractiveDashboardHtml } from '../../src/api/catalog';
import { writeDashboardHtml } from './dashboardPopup';

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
  const [dashboardError, setDashboardError] = useState('');

  async function openDashboard() {
    setDashboardError('');
    // Open synchronously from the click so popup blockers do not swallow the
    // authenticated fetch that follows. The new tab receives only verified HTML.
    const popup = window.open('about:blank', '_blank');
    if (!popup) {
      setDashboardError('The browser blocked the new tab. Allow pop-ups for SARGuardian and try again.');
      return;
    }
    try {
      popup.opener = null;
      popup.document.title = 'SARGuardian · Interactive dashboard';
      popup.document.body.textContent = 'Verifying the private result bundle and preparing the interactive dashboard…';
    } catch {
      // Continue with the navigation below; some browser privacy modes restrict
      // access to a newly opened blank document.
    }

    setOpening(true);
    try {
      const html = await getInteractiveDashboardHtml(job.id, user);
      // Keep the popup at the Vercel-origin about:blank document instead of
      // navigating to a blob: URL, preserving same-origin and CSP behavior.
      writeDashboardHtml(popup, html);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Could not open the interactive dashboard.';
      setDashboardError(message);
      try {
        popup.document.open();
        popup.document.write('<!doctype html><html lang="en"><meta charset="utf-8"><title>SARGuardian dashboard unavailable</title><body style="font:16px system-ui;padding:24px"><h1>Dashboard unavailable</h1><p id="message"></p></body></html>');
        const messageNode = popup.document.getElementById('message');
        if (messageNode) messageNode.textContent = message;
        popup.document.close();
      } catch {
        // The in-app status below still reports the failure when the popup is
        // not writable (for example, if navigation already began).
      }
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
            onClick={() => { void openDashboard(); }}
            disabled={opening}
          >
            {opening ? 'Opening dashboard…' : 'Open interactive dashboard'}
          </button>
        )}
        {folderUrl && (
          <a className="investigate-button validated-output-link" href={folderUrl} target="_blank" rel="noreferrer">
            Download results
          </a>
        )}
      </div>
      {canOpenDashboard && <p className="scientific-results-note">The map and playback use the verified result bundle. Processing PASS is not independent geophysical validation; limitations remain visible in the dashboard.</p>}
      {dashboardError && <p className="scientific-results-error" role="alert">{dashboardError}</p>}
    </section>
  );
}
