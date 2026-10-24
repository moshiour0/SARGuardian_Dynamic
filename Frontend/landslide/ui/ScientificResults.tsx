import type { DisplacementJob } from '../../src/api/catalog';

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

export function ScientificResults({ job }: { job: DisplacementJob }) {
  const folderUrl = driveFolderUrl(job);
  if (!folderUrl) return null;
  return <p className="scientific-results">
    <a className="investigate-button validated-output-link" href={folderUrl} target="_blank" rel="noreferrer">
      Download results
    </a>
  </p>;
}
