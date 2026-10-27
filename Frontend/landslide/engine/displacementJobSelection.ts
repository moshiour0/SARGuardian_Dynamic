import type { DisplacementJob, DisplacementProcessingOptions } from '../../src/api/catalog';
import type { PreparedInvestigation } from './investigation';

const activeStates = new Set(['queued', 'dispatching', 'running']);

function normalizePlaceName(value: string | null | undefined): string | null {
  const normalized = value?.trim().replace(/\s+/g, ' ');
  return normalized || null;
}

function sameOptionalCoordinate(a: number | null | undefined, b: number | null | undefined): boolean {
  if (a == null || b == null) return a == null && b == null;
  return Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) <= 1e-8;
}

/**
 * Select the current account job for an exact AOI/date/option/place/target selection.
 * A currently running account job takes priority because parallel science jobs
 * are prohibited. Once no job is active, a completed job for a different place
 * is never returned as though it belonged to this selection.
 */
export function chooseAccountJob(
  jobs: DisplacementJob[],
  preparation: PreparedInvestigation,
  options: DisplacementProcessingOptions,
): DisplacementJob | null {
  const matching = preparation.valid
    ? jobs.filter((candidate) => candidate.aoi_wkt === preparation.payload.aoi_wkt
      && candidate.start_date === preparation.payload.start_date
      && candidate.end_date === preparation.payload.end_date
      && normalizePlaceName(candidate.place_name) === normalizePlaceName(preparation.payload.place_name)
      && sameOptionalCoordinate(candidate.place_latitude, preparation.payload.place_latitude)
      && sameOptionalCoordinate(candidate.place_longitude, preparation.payload.place_longitude)
      && sameOptionalCoordinate(candidate.target_latitude, preparation.payload.target_latitude)
      && sameOptionalCoordinate(candidate.target_longitude, preparation.payload.target_longitude)
      && (candidate.product_type || 'BOTH') === options.product_type
      && (candidate.acquisition_window || 'secondary-acquisition-in-window') === options.acquisition_window
      && (candidate.retention_mode || 'derived-only') === options.retention_mode
      && (candidate.processing_profile || 'full-aoi') === options.processing_profile)
    : [];

  return matching.find((candidate) => activeStates.has(candidate.state))
    || jobs.find((candidate) => activeStates.has(candidate.state))
    || matching[0]
    || null;
}
