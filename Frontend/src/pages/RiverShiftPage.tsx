import { useEffect, useMemo, useRef, useState } from 'react';
import type { User } from 'firebase/auth';
import { importLibrary, setOptions } from '@googlemaps/js-api-loader';
import { createRiverShiftJob, downloadRiverShiftArtifact, getRiverShiftJob, listRiverShiftJobs, type RiverShiftJob } from '../api/catalog';
import type { RiverShiftGeoJSON, RiverShiftReport, RiverShiftTimeSeriesFrame } from '../riverShift/manifest';
import { countRiverShiftCandidateLayers, filterRiverShiftCandidateGeoJSON, isRiverShiftGeoJSONApproved, isRiverShiftReportVerified, isRiverShiftTimeSeriesGeoJSONApproved, parseRiverShiftGeoJSON, parseRiverShiftReport } from '../riverShift/manifest';
import { formatWktPolygon, parseWktPolygon, type Wgs84Point } from '../riverShift/aoi';
import { deriveGeneralChangeMarkers } from '../riverShift/generalChangeMarkers';
import './river-shift.css';

type Props = {
  onHome: () => void;
  user: User | null;
  firebaseConfigured: boolean;
  authState: 'idle' | 'loading' | 'ready' | 'error';
  onSignIn: () => Promise<void>;
  onSignOut: () => Promise<void>;
};

type CatalogueProduct = {
  granule_name: string;
  product_type: 'GCOV';
  processing_type?: string | null;
  crid?: string | null;
  maturity: 'BETA' | 'PROVISIONAL' | 'UNKNOWN';
  download_url?: string | null;
  start_time_utc?: string | null;
  stop_time_utc?: string | null;
  flight_direction?: string | null;
  relative_orbit?: string | null;
  cycle?: string | null;
  frame?: string | null;
  bandwidth_mode?: string | null;
  polarization_code?: string | null;
  main_band_polarization?: string | null;
  frame_coverage?: string | null;
  size_mb?: number | null;
};

type CatalogueResponse = {
  mode: 'catalogue_only';
  status: 'ready' | 'no_data' | 'upstream_error';
  message: string;
  data_downloaded: false;
  scientific_analysis_performed: false;
  product_count: number;
  products: CatalogueProduct[];
  warnings: string[];
  query: Record<string, unknown>;
};

type LoadedGeoJSON = { fileName: string; sha256: string; data: RiverShiftGeoJSON };
type LoadedJobPreview = { jobId: string; changeLayer: LoadedGeoJSON | null; report: RiverShiftReport | null; error: string };
type LoadedJobFrame = { jobId: string; date: string; data: RiverShiftGeoJSON };

const API_BASE = ((import.meta.env.VITE_SARGUARDIAN_API_URL as string | undefined) || 'http://localhost:8000')
  .trim()
  .replace(/\/+$/, '');
const GOOGLE_MAPS_KEY = (import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined) || '';
let mapsConfigured = false;

function formatDate(value?: string | null): string {
  if (!value) return 'Not supplied';
  const compact = /^(\d{4})(\d{2})(\d{2})T\d{6}(?:\.\d+)?Z?$/.exec(value);
  if (compact) return `${compact[1]}-${compact[2]}-${compact[3]}`;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString().slice(0, 10);
}

function metricLabel(key: string): string {
  const names: Record<string, string> = {
    baseline_water_area_km2: 'Baseline water area',
    event_proximal_water_area_km2: 'Event-proximal observed water area',
    comparison_one_water_area_km2: 'Comparison one water area',
    comparison_two_water_area_km2: 'Comparison two water area',
    peak_water_area_km2: 'Legacy peak-water field (not confirmed peak stage)',
    post_event_water_area_km2: 'Post-event water area',
    newly_water_covered_area_km2: 'New water coverage',
    water_area_change_percent: 'Water-area change',
    persistent_erosion_candidate_area_ha: 'Erosion candidates',
    persistent_deposition_candidate_area_ha: 'Deposition candidates',
    net_candidate_land_change_ha: 'Net candidate land change',
    symmetric_boundary_distance_mean_m: 'Mean boundary distance',
    symmetric_boundary_distance_p95_m: 'P95 boundary distance',
    accuracy: 'Accuracy',
    precision_user_accuracy: 'Precision',
    recall_producer_accuracy: 'Recall',
    f1_score: 'F1 score',
    intersection_over_union: 'Intersection over Union',
    cohen_kappa: 'Cohen’s kappa',
    area_bias_percent: 'Area bias',
    mean_signed_displacement_m: 'Mean signed bank displacement',
    median_signed_displacement_m: 'Median signed bank displacement',
    p95_absolute_displacement_m: 'P95 absolute displacement',
    mean_net_rate_m_per_year: 'Mean net movement rate',
    channelward_count: 'Channelward transects',
    landward_count: 'Landward transects',
    unchanged_count: 'Unchanged transects',
    valid_transect_count: 'Valid transects',
    invalid_transect_count: 'Invalid transects',
    transect_count: 'Total transects',
  };
  return names[key] || key.replace(/_/g, ' ');
}

function metricUnit(key: string): string {
  if (key.includes('km2')) return 'km²';
  if (key.includes('_ha')) return 'ha';
  if (key.endsWith('_m_per_year')) return 'm/year';
  if (key.endsWith('_m')) return 'm';
  if (key.includes('percent')) return '%';
  return '';
}

function metricText(key: string, value: number | null): string {
  if (value === null) return 'Unavailable';
  const digits = key.includes('percent') ? 1 : key.includes('area') || key.endsWith('_ha') || key.includes('km2') ? 3 : 3;
  return value.toFixed(digits);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function overlapNumber(record: Record<string, unknown> | null, key: string): number | null {
  const value = record?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function overlapText(value: number | null, digits = 3): string {
  return value === null ? 'Not available' : value.toFixed(digits);
}

function eventObservationTiming(summary: Record<string, unknown> | null): {
  eventDate: string;
  observationDate: string;
  offsetDays: number;
} | null {
  const timing = asRecord(summary?.event_observation_timing);
  const eventDate = timing?.event_date;
  const observationDate = timing?.first_post_event_observation;
  const offsetDays = timing?.offset_days;
  if (typeof eventDate !== 'string' || typeof observationDate !== 'string'
    || typeof offsetDays !== 'number' || !Number.isFinite(offsetDays) || offsetDays <= 0) return null;
  return { eventDate, observationDate, offsetDays };
}

function OverlapAuditPanel({ summary }: { summary: Record<string, unknown> }) {
  const sourceAudit = asRecord(summary.candidate_overlap_audit);
  const sourcePairs = asRecord(sourceAudit?.pairwise);
  const previewAudit = asRecord(summary.preview_geometry_overlap);
  const previewPairs = asRecord(previewAudit?.pairwise);
  if (!sourcePairs || !previewPairs) return null;

  const rows = [
    { key: 'erosion__deposition', label: 'Erosion ↔ Deposition' },
    { key: 'erosion__new_water', label: 'Erosion ↔ New water' },
    { key: 'deposition__new_water', label: 'Deposition ↔ New water' },
  ];

  return (
    <section className="rs-overlap-audit" aria-label="RiverShift candidate overlap audit">
      <div className="rs-section-heading">
        <div><p className="rs-kicker">SPATIAL QA</p><h3>Candidate overlap audit</h3></div>
        <span className="rs-chip">DIAGNOSTIC · NOT ACCURACY</span>
      </div>
      <p className="rs-muted">
        Full-resolution source masks are compared separately from lower-resolution map-preview footprints.
        Preview overlap can be introduced by downsampling and must not be used as a measurement.
      </p>
      <div className="rs-overlap-table-wrap">
        <table className="rs-table rs-overlap-table">
          <thead>
            <tr>
              <th>Class pair</th>
              <th>Source-mask intersection</th>
              <th>Preview intersection</th>
              <th>Source Jaccard</th>
              <th>Preview Jaccard</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const source = asRecord(sourcePairs[row.key]);
              const preview = asRecord(previewPairs[row.key]);
              return (
                <tr key={row.key}>
                  <td>{row.label}</td>
                  <td>{overlapNumber(source, 'intersection_pixels')?.toLocaleString() ?? 'Not available'} px</td>
                  <td>{overlapText(overlapNumber(preview, 'intersection_area_km2'))} km²</td>
                  <td>{overlapText(overlapNumber(source, 'jaccard'))}</td>
                  <td>{overlapText(overlapNumber(preview, 'jaccard'))}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="rs-overlay-note">
        Erosion and deposition source masks must be disjoint on common valid pixels. New-water may legitimately
        intersect a morphology class because it is derived from a different date comparison. Review the selected
        dates before interpreting any intersection as a physical contradiction.
      </p>
    </section>
  );
}

function ReportPanel({ report }: { report: RiverShiftReport }) {
  const verified = isRiverShiftReportVerified(report);
  const verification = report.quality.verification;
  const verificationRecord = verification && typeof verification === 'object'
    ? verification as Record<string, unknown>
    : null;
  const warnings = report.quality.warnings || [];
  const title = report.analysis_type === 'river_change_scan'
    ? 'General river change scan'
    : report.analysis_type === 'flood_inundation_screening'
    ? 'Flood inundation screening'
    : report.analysis_type === 'river_morphology_screening'
      ? 'River morphology screening'
      : report.analysis_type === 'water_timeseries_screening'
        ? 'Observed-date water time series'
      : report.analysis_type === 'classification_validation'
        ? 'Classification validation'
        : 'Bankline migration measurement';
  const source = report.source;
  return (
    <section className="rs-panel">
      <div className="rs-section-heading">
        <div><p className="rs-kicker">RIVER SHIFT SCIENTIFIC RESULT</p><h2>{title}</h2></div>
        <span className="rs-quality">{report.quality.status.replace(/_/g, ' ')}</span>
      </div>
      <div className="rs-provenance">
        <span>Mission: <strong>{String(source.mission || 'Not stated')}</strong></span>
        <span>Product: <strong>{String(source.product || 'Not stated')}</strong></span>
        <span>Maturity: <strong>{String(source.maturity || 'Not stated')}</strong></span>
        <span>Frequency: <strong>{String(source.frequency || 'Not stated')}</strong></span>
        <span>Term: <strong>{String(source.covariance_term || 'Not stated')}</strong></span>
      </div>
      <div className="rs-warning-inline" role="status">
        Release status: <strong>{String(report.quality.release_status || 'PROVISIONAL')}</strong>
        {!verified && <span> · The metrics and map are provisional evidence, not independently validated findings.</span>}
      </div>
      <div className="rs-metric-grid">
        {Object.entries(report.metrics).map(([key, value]) => (
          <article className="rs-metric" key={key}>
            <span>{metricLabel(key)}</span>
            <strong>{metricText(key, value)} <small>{metricUnit(key)}</small></strong>
          </article>
        ))}
      </div>
      {verified && verificationRecord && (
        <div className="rs-reference">
          Review record: <strong>{String(verificationRecord.reviewer_id)}</strong>
          <span>{String(verificationRecord.reviewed_at_utc)}</span>
          <p>Evidence: {String(verificationRecord.evidence_reference)}</p>
        </div>
      )}
      <div className="rs-warning-list">
        <strong>Input provenance</strong>
        <p>Granules: {Array.isArray(source.granule_names) && source.granule_names.length ? source.granule_names.join(', ') : 'No NISAR granule IDs recorded'}</p>
        <p>Source checksums: {Array.isArray(source.source_granule_sha256) && source.source_granule_sha256.length ? source.source_granule_sha256.join(', ') : 'No source product checksums recorded'}</p>
        {report.inputs && <p>Input checksums: {JSON.stringify(report.inputs.input_sha256 || 'Not recorded')}</p>}
      </div>
      {report.reference && (
        <div className="rs-reference">
          Reference: <strong>{String(report.reference.source || 'Not stated')}</strong>
          <span>{String(report.reference.date || 'Date not stated')}</span>
          <p>{String(report.reference.method || 'Reference method not stated')}</p>
        </div>
      )}
      {report.uncertainty && (
        <details className="rs-reference">
          <summary>Uncertainty and sensitivity details</summary>
          <pre>{JSON.stringify(report.uncertainty, null, 2)}</pre>
        </details>
      )}
      {warnings.length > 0 && (
        <div className="rs-warning-list">
          <strong>Scientific interpretation and limitations</strong>
          {warnings.map((warning, index) => <p key={index}>• {warning}</p>)}
        </div>
      )}
    </section>
  );
}

export function RiverShiftPage({ onHome, user, firebaseConfigured, authState, onSignIn, onSignOut }: Props) {
  const [aoiWkt, setAoiWkt] = useState('');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [maturity, setMaturity] = useState<'BETA' | 'PROVISIONAL' | 'ALL'>('PROVISIONAL');
  const [polarization, setPolarization] = useState<'' | 'HH' | 'HH+HV' | 'HH+HV+VH+VV' | 'VV' | 'VV+VH' | 'LH+LV' | 'RH+RV'>('');
  const [direction, setDirection] = useState<'' | 'ASCENDING' | 'DESCENDING'>('');
  const [frameCoverage, setFrameCoverage] = useState<'ALL' | 'FULL' | 'PARTIAL'>('ALL');
  const [catalogue, setCatalogue] = useState<CatalogueResponse | null>(null);
  const [catalogueBusy, setCatalogueBusy] = useState(false);
  const [catalogueError, setCatalogueError] = useState('');
  const [report, setReport] = useState<RiverShiftReport | null>(null);
  const [reportError, setReportError] = useState('');
  const [geoJSONFiles, setGeoJSONFiles] = useState<LoadedGeoJSON[]>([]);
  const [geoJSONError, setGeoJSONError] = useState('');
  const [mapError, setMapError] = useState('');
  const [mapReady, setMapReady] = useState(false);
  const [drawingAoi, setDrawingAoi] = useState(false);
  const [aoiVertices, setAoiVertices] = useState<Wgs84Point[]>([]);
  const [aoiError, setAoiError] = useState('');
  const [timeSeriesIndex, setTimeSeriesIndex] = useState(0);
  const [timeSeriesPlaying, setTimeSeriesPlaying] = useState(false);
  const [analysisMode, setAnalysisMode] = useState<'general_scan' | 'event_based'>('general_scan');
  const [eventDate, setEventDate] = useState('');
  const [scienceFrequency, setScienceFrequency] = useState<'frequencyA' | 'frequencyB'>('frequencyA');
  const [covarianceTerm, setCovarianceTerm] = useState('HHHH');
  const [thresholdMethod, setThresholdMethod] = useState<'otsu' | 'manual'>('otsu');
  const [thresholdDb, setThresholdDb] = useState('');
  const [riverJob, setRiverJob] = useState<RiverShiftJob | null>(null);
  const [riverJobBusy, setRiverJobBusy] = useState(false);
  const [riverJobError, setRiverJobError] = useState('');
  const [jobPreview, setJobPreview] = useState<LoadedJobPreview | null>(null);
  const [jobFrame, setJobFrame] = useState<LoadedJobFrame | null>(null);
  const [jobTimeSeriesIndex, setJobTimeSeriesIndex] = useState(0);
  const [jobTimeSeriesPlaying, setJobTimeSeriesPlaying] = useState(false);
  const [showGeneralChangeMarkers, setShowGeneralChangeMarkers] = useState(false);
  const [showErosionOverlay, setShowErosionOverlay] = useState(true);
  const [showDepositionOverlay, setShowDepositionOverlay] = useState(true);
  const [showNewWaterOverlay, setShowNewWaterOverlay] = useState(true);
  const [showObservedWaterExtent, setShowObservedWaterExtent] = useState(true);
  const aoiMapElement = useRef<HTMLDivElement>(null);
  const resultMapElement = useRef<HTMLDivElement>(null);
  const aoiMapInstance = useRef<google.maps.Map | null>(null);
  const resultMapInstance = useRef<google.maps.Map | null>(null);
  const resultMapFitKey = useRef('');
  const resultAoiPolygon = useRef<google.maps.Polygon | null>(null);
  const aoiPolygon = useRef<google.maps.Polygon | null>(null);
  const aoiPreviewLine = useRef<google.maps.Polyline | null>(null);
  const timeSeriesFrames = useMemo(() => (report?.time_series || []).flatMap((frame) => {
    const loaded = geoJSONFiles.find((file) => file.fileName === frame.water_geojson
      && file.sha256.toLowerCase() === frame.water_geojson_sha256.toLowerCase());
    return loaded && isRiverShiftTimeSeriesGeoJSONApproved(report!, frame.date, loaded.sha256)
      ? [{ ...frame, data: loaded.data }]
      : [];
  }), [report, geoJSONFiles]);
  const playbackFrames = report?.analysis_type === 'water_timeseries_screening'
    && report.time_series?.length === timeSeriesFrames.length ? timeSeriesFrames : [];
  const currentTimeSeriesFrame = playbackFrames[timeSeriesIndex];
  const jobTimeSeriesFrames: RiverShiftTimeSeriesFrame[] = jobPreview && riverJob && jobPreview.jobId === riverJob.id
    && jobPreview.report?.analysis_type === 'water_timeseries_screening'
    ? jobPreview.report.time_series || [] : [];
  const currentJobFrameMetadata = jobTimeSeriesFrames[jobTimeSeriesIndex];
  const riverJobId = riverJob?.id;
  const riverJobArtifactManifest = riverJob?.artifact_manifest;
  const currentJobFrameDate = currentJobFrameMetadata?.date;
  const currentJobFrameGeoJSON = currentJobFrameMetadata?.water_geojson;
  const currentJobFrameSHA256 = currentJobFrameMetadata?.water_geojson_sha256;
  const jobFrameJobId = jobFrame?.jobId;
  const jobFrameDate = jobFrame?.date;
  const currentJobFrame = currentJobFrameMetadata && riverJobId && jobFrame && jobFrame.jobId === riverJobId
    && jobFrame.date === currentJobFrameMetadata.date ? jobFrame.data : null;
  const hasJobPreviewOverlay = Boolean(jobPreview && riverJobId && jobPreview.jobId === riverJobId
    && (jobPreview.changeLayer || currentJobFrame));
  const activeJobPreview = jobPreview && riverJobId && jobPreview.jobId === riverJobId ? jobPreview : null;
  const eventTiming = eventObservationTiming(riverJob?.result_summary || null);
  const isGeneralScanJob = Boolean(riverJob?.state === 'succeeded' && riverJob.analysis_mode === 'general_scan');
  const generalChangeMarkers = useMemo(() => (
    isGeneralScanJob && activeJobPreview?.changeLayer
      ? deriveGeneralChangeMarkers(activeJobPreview.changeLayer.data)
      : []
  ), [isGeneralScanJob, activeJobPreview?.changeLayer]);
  const hasLiveJobResult = Boolean(riverJob?.state === 'succeeded' && activeJobPreview
    && (activeJobPreview.report || activeJobPreview.changeLayer));
  const hasLiveJobResultPending = Boolean(riverJob?.state === 'succeeded' && riverJobArtifactManifest?.length
    && !hasLiveJobResult);
  const explorerReport = report ?? activeJobPreview?.report ?? null;
  const activeJobPreviewError = activeJobPreview?.error || '';
  const verifiedGeoJSONFiles = useMemo(() => {
    if (!report || report.analysis_type === 'water_timeseries_screening') return [];
    return geoJSONFiles.filter((file) => isRiverShiftGeoJSONApproved(report, file.sha256));
  }, [report, geoJSONFiles]);
  const candidateOverlayVisibility = useMemo(() => ({
    erosion: showErosionOverlay,
    deposition: showDepositionOverlay,
    newWater: showNewWaterOverlay,
  }), [showErosionOverlay, showDepositionOverlay, showNewWaterOverlay]);
  const candidateOverlayCounts = useMemo(() => {
    const sources = activeJobPreview?.changeLayer
      ? [activeJobPreview.changeLayer.data]
      : verifiedGeoJSONFiles.map((file) => file.data);
    return sources.reduce((total, source) => {
      const counts = countRiverShiftCandidateLayers(source);
      return {
        erosion: total.erosion + counts.erosion,
        deposition: total.deposition + counts.deposition,
        newWater: total.newWater + counts.newWater,
        unclassified: total.unclassified + counts.unclassified,
      };
    }, { erosion: 0, deposition: 0, newWater: 0, unclassified: 0 });
  }, [activeJobPreview?.changeLayer, verifiedGeoJSONFiles]);
  const hasCandidateOverlaySource = Boolean(activeJobPreview?.changeLayer || verifiedGeoJSONFiles.length);
  const allCandidateLayersVisible = showErosionOverlay && showDepositionOverlay && showNewWaterOverlay;
  const hasObservedWaterFrames = Boolean(currentTimeSeriesFrame || jobTimeSeriesFrames.length);
  const hasVerifiedOverlay = Boolean(!hasJobPreviewOverlay && ((currentTimeSeriesFrame && showObservedWaterExtent) || verifiedGeoJSONFiles.length));

  useEffect(() => {
    if (!user) {
      setRiverJob(null);
      return undefined;
    }
    let cancelled = false;
    void listRiverShiftJobs(user, 10).then((jobs) => {
      if (!cancelled && jobs.length) {
        setRiverJob((current) => current ?? jobs[0]);
      }
    }).catch((error: unknown) => {
      if (!cancelled) {
        setRiverJobError(error instanceof Error ? error.message : 'Could not restore the last River Shift job.');
      }
    });
    return () => { cancelled = true; };
  }, [user]);

  useEffect(() => {
    if (!user || !riverJob || !['queued', 'dispatching', 'running'].includes(riverJob.state)) return undefined;
    const timer = window.setInterval(() => {
      void getRiverShiftJob(riverJob.id, user).then(setRiverJob).catch((error: unknown) => {
        setRiverJobError(error instanceof Error ? error.message : 'Could not refresh processing status.');
      });
    }, 5000);
    return () => window.clearInterval(timer);
  }, [riverJob, user]);

  useEffect(() => {
    if (!user || riverJob?.state !== 'succeeded' || !riverJob.artifact_manifest?.length) {
      setJobPreview(null);
      setJobFrame(null);
      setJobTimeSeriesIndex(0);
      setJobTimeSeriesPlaying(false);
      return undefined;
    }
    let cancelled = false;
    const loadPreview = async () => {
      const artifacts = riverJob.artifact_manifest || [];
      let changeLayer: LoadedGeoJSON | null = null;
      let timeseriesReport: RiverShiftReport | null = null;
      let failure = '';
      const changeIndex = artifacts.findIndex((item) => item.name === 'river_change_candidates_preview.geojson' && item.bytes <= 4_000_000);
      const reportIndex = artifacts.findIndex((item) => item.name === 'river_shift_timeseries_preview.json' && item.bytes <= 4_000_000);
      if (changeIndex >= 0) {
        try {
          const artifact = artifacts[changeIndex];
          const blob = await downloadRiverShiftArtifact(riverJob.id, changeIndex, user);
          const bytes = await blob.arrayBuffer();
          const digest = await crypto.subtle.digest('SHA-256', bytes);
          const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
          if (sha256.toLowerCase() !== artifact.sha256.toLowerCase()) throw new Error('Map preview checksum did not match its private result receipt.');
          changeLayer = {
            fileName: artifact.name,
            sha256,
            data: parseRiverShiftGeoJSON(JSON.parse(new TextDecoder().decode(bytes)) as unknown),
          };
        } catch (error) {
          failure = error instanceof Error ? error.message : 'Could not load the provisional map preview.';
        }
      }
      if (reportIndex >= 0) {
        try {
          const artifact = artifacts[reportIndex];
          const blob = await downloadRiverShiftArtifact(riverJob.id, reportIndex, user);
          const bytes = await blob.arrayBuffer();
          const digest = await crypto.subtle.digest('SHA-256', bytes);
          const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
          if (sha256.toLowerCase() !== artifact.sha256.toLowerCase()) throw new Error('Time-series report checksum did not match its private result receipt.');
          timeseriesReport = parseRiverShiftReport(JSON.parse(new TextDecoder().decode(bytes)) as unknown);
        } catch (error) {
          failure = failure || (error instanceof Error ? error.message : 'Could not load the observed-date time series.');
        }
      }
      if (!cancelled) {
        setJobPreview({ jobId: riverJob.id, changeLayer, report: timeseriesReport, error: failure });
        setJobTimeSeriesIndex(0);
      }
    };
    void loadPreview();
    return () => { cancelled = true; };
  }, [riverJob?.id, riverJob?.state, riverJob?.artifact_manifest, user]);

  useEffect(() => {
    if (!user || !riverJobId || !currentJobFrameDate || !currentJobFrameGeoJSON || !currentJobFrameSHA256
      || (jobFrameJobId === riverJobId && jobFrameDate === currentJobFrameDate)) return undefined;
    const artifacts = riverJobArtifactManifest || [];
    const artifactIndex = artifacts.findIndex((item) => item.name === currentJobFrameGeoJSON && item.bytes <= 4_000_000);
    if (artifactIndex < 0) {
      setJobTimeSeriesPlaying(false);
      setJobPreview((current) => current?.jobId === riverJobId
        ? { ...current, error: `The private result manifest is missing the ${currentJobFrameGeoJSON} frame artifact.` }
        : current);
      return undefined;
    }
    let cancelled = false;
    const loadFrame = async () => {
      try {
        const artifact = artifacts[artifactIndex];
        const blob = await downloadRiverShiftArtifact(riverJobId, artifactIndex, user);
        const bytes = await blob.arrayBuffer();
        const digest = await crypto.subtle.digest('SHA-256', bytes);
        const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
        if (sha256.toLowerCase() !== artifact.sha256.toLowerCase()
          || sha256.toLowerCase() !== currentJobFrameSHA256.toLowerCase()) {
          throw new Error('Observed-date preview checksum did not match the report and private result receipt.');
        }
        const data = parseRiverShiftGeoJSON(JSON.parse(new TextDecoder().decode(bytes)) as unknown);
        if (!cancelled) setJobFrame({ jobId: riverJobId, date: currentJobFrameDate, data });
      } catch (error) {
        if (!cancelled) {
          setJobTimeSeriesPlaying(false);
          setJobPreview((current) => current?.jobId === riverJobId
            ? { ...current, error: error instanceof Error ? error.message : 'Could not load this observed-date preview.' }
            : current);
        }
      }
    };
    void loadFrame();
    return () => { cancelled = true; };
  }, [riverJobId, riverJobArtifactManifest, user, currentJobFrameDate, currentJobFrameGeoJSON, currentJobFrameSHA256, jobFrameJobId, jobFrameDate]);

  const startLiveProcessing = async () => {
    setRiverJobError('');
    if (!user) {
      setRiverJobError('Sign in with Google before starting live processing.');
      return;
    }
    if (!aoiWkt.trim() || !startDate || !endDate || endDate < startDate) {
      setRiverJobError('Draw or paste one valid AOI and choose a valid start/end date window.');
      return;
    }
    if ((new Date(endDate).getTime() - new Date(startDate).getTime()) / 86400000 > 366) {
      setRiverJobError('The NISAR observation window cannot exceed 366 days.');
      return;
    }
    if (analysisMode === 'event_based' && (!eventDate || eventDate < startDate || eventDate > endDate)) {
      setRiverJobError('Event-Based Analysis needs an independently sourced event date inside the selected observation window.');
      return;
    }
    if (analysisMode === 'general_scan' && eventDate) {
      setRiverJobError('General River Change Scan does not accept an event date. Clear it or switch to Event-Based Analysis.');
      return;
    }
    if (maturity === 'ALL') {
      setRiverJobError('Choose one maturity. BETA and PROVISIONAL products cannot be mixed in a scientific time series.');
      return;
    }
    if (thresholdMethod === 'manual') {
      const manualThreshold = Number(thresholdDb);
      if (!thresholdDb.trim() || !Number.isFinite(manualThreshold) || manualThreshold < -60 || manualThreshold > 0) {
        setRiverJobError('Enter one manual threshold between -60 and 0 dB, or choose Otsu.');
        return;
      }
    }
    setRiverJobBusy(true);
    setJobPreview(null);
    setJobFrame(null);
    setJobTimeSeriesIndex(0);
    setJobTimeSeriesPlaying(false);
    setShowGeneralChangeMarkers(false);
    try {
      const job = await createRiverShiftJob({
        aoiWkt, startDate, endDate, analysisMode,
        eventDate: analysisMode === 'event_based' ? eventDate : null,
        maturity,
        mainBandPolarization: polarization || null,
        flightDirection: direction || null,
        frameCoverage,
        frequency: scienceFrequency, covarianceTerm, thresholdMethod,
        ...(thresholdMethod === 'manual' ? { thresholdDb: Number(thresholdDb) } : {}),
        idempotencyKey: crypto.randomUUID().replace(/-/g, ''),
      }, user);
      setRiverJob(job);
    } catch (error) {
      setRiverJobError(error instanceof Error ? error.message : 'Could not start live RiverShift processing.');
    } finally {
      setRiverJobBusy(false);
    }
  };

  const saveRiverArtifact = async (artifactIndex: number, fileName: string) => {
    if (!riverJob || !user) return;
    setRiverJobError('');
    try {
      const blob = await downloadRiverShiftArtifact(riverJob.id, artifactIndex, user);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = fileName;
      link.click();
      URL.revokeObjectURL(objectUrl);
    } catch (error) {
      setRiverJobError(error instanceof Error ? error.message : 'Could not download this private result.');
    }
  };

  const searchCatalogue = async () => {
    setCatalogueError('');
    setCatalogue(null);
    setCatalogueBusy(true);
    try {
      const coordinates = parseWktPolygon(aoiWkt);
      const response = await fetch(API_BASE + '/api/river-shift/catalog/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          coordinates,
          start_date: startDate,
          end_date: endDate,
          maturity,
          main_band_polarization: polarization || null,
          flight_direction: direction || null,
          frame_coverage: frameCoverage,
          max_results: 100,
        }),
      });
      const payload = await response.json() as CatalogueResponse | { detail?: string };
      if (!response.ok) {
        const detail = 'detail' in payload && payload.detail ? payload.detail : 'Catalogue request failed (' + response.status + ').';
        throw new Error(detail);
      }
      setCatalogue(payload as CatalogueResponse);
    } catch (error) {
      setCatalogueError(error instanceof Error ? error.message : 'Could not search the ASF catalogue.');
    } finally {
      setCatalogueBusy(false);
    }
  };

  const loadReport = async (file?: File) => {
    if (!file) return;
    setReportError('');
    setReport(null);
    setGeoJSONFiles([]);
    setTimeSeriesIndex(0);
    setTimeSeriesPlaying(false);
    try {
      const payload = JSON.parse(await file.text()) as unknown;
      setReport(parseRiverShiftReport(payload));
    } catch (error) {
      setReportError(error instanceof Error ? error.message : 'Could not read the result JSON.');
    }
  };

  const loadGeoJSONFiles = async (files?: FileList | File[]) => {
    if (!files || !files.length) return;
    setGeoJSONError('');
    try {
      const loaded = await Promise.all(Array.from(files).map(async (file) => {
        const bytes = await file.arrayBuffer();
        const digest = await crypto.subtle.digest('SHA-256', bytes);
        const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
        const payload = JSON.parse(new TextDecoder().decode(bytes)) as unknown;
        return { fileName: file.name, sha256, data: parseRiverShiftGeoJSON(payload) };
      }));
      setGeoJSONFiles(loaded);
      setTimeSeriesIndex(0);
      setTimeSeriesPlaying(false);
    } catch (error) {
      setGeoJSONError(error instanceof Error ? error.message : 'Could not read the GeoJSON.');
    }
  };

  const finishAoiDrawing = () => {
    try {
      setAoiWkt(formatWktPolygon(aoiVertices));
      setAoiError('');
      setDrawingAoi(false);
      setAoiVertices([]);
    } catch (error) {
      setAoiError(error instanceof Error ? error.message : 'Could not finish the AOI polygon.');
    }
  };

  useEffect(() => {
    if (!GOOGLE_MAPS_KEY) {
      setMapError('Google Maps API key is not configured. Add it to Frontend/.env.local and restart Vite to enable AOI drawing and map overlays.');
      return;
    }
    let cancelled = false;
    if (!mapsConfigured) {
      setOptions({ key: GOOGLE_MAPS_KEY, v: 'weekly' });
      mapsConfigured = true;
    }
    importLibrary('maps').then((mapsLibrary) => {
      if (cancelled) return;
      const maps = mapsLibrary as google.maps.MapsLibrary;
      const mapOptions: google.maps.MapOptions = {
        center: { lat: 24.6, lng: 89.65 },
        zoom: 8,
        mapTypeId: 'satellite',
        streetViewControl: false,
        mapTypeControl: true,
        fullscreenControl: true,
      };
      if (aoiMapElement.current && !aoiMapInstance.current) {
        aoiMapInstance.current = new maps.Map(aoiMapElement.current, mapOptions);
      }
      if (resultMapElement.current && !resultMapInstance.current) {
        resultMapInstance.current = new maps.Map(resultMapElement.current, mapOptions);
      }
      if (aoiMapInstance.current && resultMapInstance.current) setMapReady(true);
    }).catch((error: unknown) => {
      if (!cancelled) setMapError(error instanceof Error ? error.message : 'Google Maps could not be loaded.');
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const map = aoiMapInstance.current;
    if (!mapReady || !map || !drawingAoi) return;
    const listener = map.addListener('click', (event: google.maps.MapMouseEvent) => {
      if (event.latLng) setAoiVertices((vertices) => [...vertices, [event.latLng!.lng(), event.latLng!.lat()]]);
    });
    return () => listener.remove();
  }, [mapReady, drawingAoi]);

  useEffect(() => {
    if (!mapReady) return;
    const map = aoiMapInstance.current;
    if (!map) return;
    if (aoiPolygon.current) aoiPolygon.current.setMap(null);
    aoiPolygon.current = null;
    let points: Wgs84Point[] = [];
    try {
      points = drawingAoi ? aoiVertices : parseWktPolygon(aoiWkt).slice(0, -1);
    } catch {
      points = drawingAoi ? aoiVertices : [];
    }
    if (points.length >= 3) {
      if (aoiPreviewLine.current) aoiPreviewLine.current.setMap(null);
      aoiPolygon.current = new google.maps.Polygon({
        paths: points.map(([lng, lat]) => ({ lng, lat })),
        strokeColor: '#70eee0',
        strokeOpacity: 0.95,
        strokeWeight: 3,
        fillColor: '#51d9cc',
        fillOpacity: 0.16,
        clickable: false,
      });
      aoiPolygon.current.setMap(map);
    } else if (points.length > 0) {
      if (!aoiPreviewLine.current) aoiPreviewLine.current = new google.maps.Polyline({
        strokeColor: '#70eee0', strokeOpacity: 0.95, strokeWeight: 3,
      });
      aoiPreviewLine.current.setPath(points.map(([lng, lat]) => ({ lng, lat })));
      aoiPreviewLine.current.setMap(map);
    } else if (aoiPreviewLine.current) {
      aoiPreviewLine.current.setMap(null);
    }
    if (aoiWkt && !drawingAoi) {
      const bounds = new google.maps.LatLngBounds();
      points.forEach(([lng, lat]) => bounds.extend({ lng, lat }));
      if (!bounds.isEmpty()) map.fitBounds(bounds);
    }
  }, [mapReady, drawingAoi, aoiVertices, aoiWkt]);

  useEffect(() => {
    if (!mapReady) return;
    const map = resultMapInstance.current;
    if (!map) return;
    if (resultAoiPolygon.current) resultAoiPolygon.current.setMap(null);
    resultAoiPolygon.current = null;
    if (isGeneralScanJob && riverJob?.aoi_wkt) {
      try {
        const aoiPoints = parseWktPolygon(riverJob.aoi_wkt).slice(0, -1);
        resultAoiPolygon.current = new google.maps.Polygon({
          paths: aoiPoints.map(([lng, lat]) => ({ lng, lat })),
          strokeColor: '#70eee0',
          strokeOpacity: 0.98,
          strokeWeight: 3,
          fillOpacity: 0,
          clickable: false,
          zIndex: 1000,
        });
        resultAoiPolygon.current.setMap(map);
      } catch {
        resultAoiPolygon.current = null;
      }
    }
  }, [mapReady, isGeneralScanJob, riverJob?.id, riverJob?.aoi_wkt]);

  useEffect(() => {
    if (!mapReady) return;
    const map = resultMapInstance.current;
    if (!map) return;
    map.data.forEach((feature) => map.data.remove(feature));
    const displayLayers = hasJobPreviewOverlay && jobPreview && riverJobId && jobPreview.jobId === riverJobId
      ? [
        jobPreview.changeLayer
          ? filterRiverShiftCandidateGeoJSON(jobPreview.changeLayer.data, candidateOverlayVisibility)
          : null,
        showObservedWaterExtent ? currentJobFrame : null,
      ].filter((layer): layer is RiverShiftGeoJSON => Boolean(layer && layer.features.length))
      : currentTimeSeriesFrame
        ? (showObservedWaterExtent ? [currentTimeSeriesFrame.data] : [])
        : verifiedGeoJSONFiles.map((file) => filterRiverShiftCandidateGeoJSON(file.data, candidateOverlayVisibility));
    displayLayers.forEach((layer) => map.data.addGeoJson(layer as unknown as object));
    if (isGeneralScanJob && showGeneralChangeMarkers && generalChangeMarkers.length > 0) {
      map.data.addGeoJson({
        type: 'FeatureCollection',
        features: generalChangeMarkers.map((marker) => ({
          type: 'Feature',
          properties: {
            general_change_marker: true,
            marker_change_type: marker.changeType,
            marker_id: marker.id,
          },
          geometry: { type: 'Point', coordinates: [marker.longitude, marker.latitude] },
        })),
      } as unknown as object);
    }
    map.data.setStyle((feature) => {
      const geometryType = feature.getGeometry()?.getType();
      const name = String(feature.getProperty('marker_change_type') || feature.getProperty('river_shift_layer') || feature.getProperty('class') || feature.getProperty('change_type') || '').toLowerCase();
      const isObservedOpenWater = name.includes('open_water') && !name.includes('new');
      const colour = name.includes('erosion') ? '#f07462'
        : name.includes('deposition') || name.includes('accretion') ? '#5fd7a5'
          : name.includes('new_water') || name.includes('newly_water') || name.includes('water_covered') ? '#53b9f4'
            : isObservedOpenWater ? '#73b3df' : '#9aafbb';
      if (geometryType === 'Point' && feature.getProperty('general_change_marker') === true) {
        return {
          icon: {
            path: google.maps.SymbolPath.CIRCLE,
            scale: 7.5,
            fillColor: colour,
            fillOpacity: 1,
            strokeColor: '#ffffff',
            strokeWeight: 1.5,
          },
          zIndex: 1000,
        };
      }
      return {
        fillColor: colour,
        strokeColor: colour,
        fillOpacity: isObservedOpenWater ? 0.14 : isGeneralScanJob ? 0.42 : 0.34,
        strokeOpacity: isObservedOpenWater ? 0.72 : 0.95,
        strokeWeight: isObservedOpenWater ? 1.2 : isGeneralScanJob ? 2.4 : 2,
      };
    });
    const bounds = new google.maps.LatLngBounds();
    map.data.forEach((feature) => {
      const geometry = feature.getGeometry();
      if (geometry) geometry.forEachLatLng((point) => bounds.extend(point));
    });
    if (isGeneralScanJob && riverJob?.aoi_wkt) {
      try {
        parseWktPolygon(riverJob.aoi_wkt).slice(0, -1)
          .forEach(([longitude, latitude]) => bounds.extend({ lng: longitude, lat: latitude }));
      } catch {
        // The result map still shows available geometry if a retained AOI cannot be parsed.
      }
    }
    const overlayFitKey = hasJobPreviewOverlay && riverJobId
      ? `job:${riverJobId}`
      : `report:${report?.analysis_type || 'none'}:${verifiedGeoJSONFiles.map((file) => file.sha256).join(',')}`;
    // Keep the camera stable while the user watches observed acquisition dates advance.
    if (!bounds.isEmpty() && resultMapFitKey.current !== overlayFitKey) {
      map.fitBounds(bounds);
      resultMapFitKey.current = overlayFitKey;
    }

  }, [mapReady, currentTimeSeriesFrame, verifiedGeoJSONFiles, jobPreview, currentJobFrame, riverJobId, hasJobPreviewOverlay, report, isGeneralScanJob, riverJob?.aoi_wkt, showGeneralChangeMarkers, generalChangeMarkers, candidateOverlayVisibility, showObservedWaterExtent]);

  useEffect(() => {
    if (!timeSeriesPlaying || playbackFrames.length < 2) return;
    const timer = window.setInterval(() => {
      setTimeSeriesIndex((index) => (index + 1) % playbackFrames.length);
    }, 1800);
    return () => window.clearInterval(timer);
  }, [timeSeriesPlaying, playbackFrames.length]);

  useEffect(() => {
    if (!jobTimeSeriesPlaying || jobTimeSeriesFrames.length < 2) return;
    // Job frames are fetched from private storage on demand. Do not advance
    // until the current frame has passed SHA-256 verification and is visible.
    if (
      !currentJobFrameMetadata
      || !currentJobFrame
      || jobFrameJobId !== riverJobId
      || jobFrameDate !== currentJobFrameMetadata.date
    ) return;
    const timer = window.setTimeout(() => {
      setJobTimeSeriesIndex((index) => (index + 1) % jobTimeSeriesFrames.length);
    }, 1800);
    return () => window.clearTimeout(timer);
  }, [
    jobTimeSeriesPlaying,
    jobTimeSeriesFrames.length,
    riverJobId,
    currentJobFrameMetadata,
    currentJobFrame,
    jobFrameJobId,
    jobFrameDate,
  ]);

  const resultType = explorerReport ? explorerReport.analysis_type.replace(/_/g, ' ') : 'No result loaded';

  return (
    <main className="river-shift-page">
      <header className="rs-header">
        <button className="rs-back" onClick={onHome} type="button">← SARGuardian</button>
        <div className="rs-brand">
          <span className="rs-brand-mark">RS</span>
          <div><strong>RiverShift</strong><small>FLOOD & RIVER CHANGE INTELLIGENCE</small></div>
        </div>
        <div className="rs-header-actions">
          <span className="rs-challenge-tag">NASA SPACE APPS 2026 · DANCING WITH THE SARS</span>
          {user
            ? <button className="rs-secondary-button" type="button" onClick={() => void onSignOut()}>{user.displayName || 'Account'} · Sign out</button>
            : <button className="rs-secondary-button" type="button" disabled={!firebaseConfigured || authState === 'loading'} onClick={() => void onSignIn()}>{firebaseConfigured ? 'Sign in to process' : 'Sign-in setup required'}</button>}
        </div>
      </header>

      <section className="rs-hero">
        <div>
          <p className="rs-kicker">NISAR-POWERED SURFACE CHANGE MONITORING</p>
          <h1>Track the river.<br /><em>Measure the change.</em></h1>
          <p className="rs-intro">
            Discover real NISAR GCOV acquisitions, inspect reproducible RiverShift analysis reports,
            and compare observed floodwater and persistent river-change candidates.
          </p>
          <div className="rs-hero-tags">
            <span>NASA-ISRO NISAR</span><span>GCOV · L-BAND</span><span>ASF DAAC</span>
          </div>
        </div>
        <div className="rs-hero-visual" aria-hidden="true">
          <div className="rs-river-contour rs-contour-one" />
          <div className="rs-river-contour rs-contour-two" />
          <div className="rs-river-contour rs-contour-three" />
          <div className="rs-river-dot rs-dot-one" /><div className="rs-river-dot rs-dot-two" /><div className="rs-river-dot rs-dot-three" />
          <span>RIVER SYSTEM<br />CHANGE OBSERVATORY</span>
        </div>
      </section>

      <section className="rs-workspace-grid">
        <div className="rs-panel">
          <div className="rs-section-heading">
            <div><p className="rs-kicker">01 · DATA DISCOVERY</p><h2>Find NISAR GCOV acquisitions</h2></div>
            <span className="rs-chip">LIVE ASF CATALOGUE</span>
          </div>
          <p className="rs-muted">
            Draw your AOI, set the observation window and choose Run. RiverShift discovers compatible NASA GCOV acquisitions, selects scenes, dispatches processing and loads the retained outputs automatically. An ASF catalogue preview is optional.
          </p>
          <form onSubmit={(event) => { event.preventDefault(); void startLiveProcessing(); }} className="rs-form">
            <section className="rs-aoi-draw" aria-label="Draw study area on the map">
              <div className="rs-section-heading">
                <div><p className="rs-kicker">STUDY AREA</p><h3>Draw AOI on satellite map</h3></div>
              </div>
              <p className="rs-muted">Click the map to add boundary vertices in order, then finish the polygon. The same WGS84 AOI is used for catalogue search.</p>
              <div className="rs-map rs-aoi-map" ref={aoiMapElement} role="application" aria-label="Click-to-draw AOI map" />
              {mapError && <p className="rs-warning-inline" role="status">{mapError}</p>}
              <div className="rs-aoi-controls">
                <button className="rs-secondary-button" type="button" disabled={!GOOGLE_MAPS_KEY || !mapReady || drawingAoi}
                  onClick={() => { setAoiError(''); setAoiVertices([]); setDrawingAoi(true); }}>
                  {drawingAoi ? 'Drawing… click the map' : 'Start drawing'}
                </button>
                <button className="rs-secondary-button" type="button" disabled={!drawingAoi || aoiVertices.length < 3} onClick={finishAoiDrawing}>Finish AOI</button>
                <button className="rs-secondary-button" type="button" onClick={() => { setDrawingAoi(false); setAoiVertices([]); setAoiWkt(''); setAoiError(''); }}>Clear</button>
                <span>{drawingAoi ? `${aoiVertices.length} vertex/vertices added` : 'Click “Start drawing” to place vertices'}</span>
              </div>
              {aoiError && <p className="rs-error" role="alert">{aoiError}</p>}
              {!GOOGLE_MAPS_KEY && <p className="rs-warning-inline">Set VITE_GOOGLE_MAPS_API_KEY in Frontend/.env.local and restart the frontend to enable the map.</p>}
            </section>
            <label className="rs-field rs-field-full">
              <span>Study area · WGS84 Polygon WKT</span>
              <textarea value={aoiWkt} onChange={(event) => setAoiWkt(event.target.value)}
                placeholder="Paste a closed POLYGON ((longitude latitude, ...)) from ASF Vertex."
                rows={4} required />
              <small>Coordinates are longitude, latitude. Use the exact same AOI for every selected acquisition.</small>
            </label>
            <div className="rs-fields-two">
              <label className="rs-field"><span>Start date</span><input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} required /></label>
              <label className="rs-field"><span>End date</span><input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} required /></label>
              <label className="rs-field rs-field-full"><span>Analysis mode</span>
                <select value={analysisMode} onChange={(event) => { setAnalysisMode(event.target.value as typeof analysisMode); if (event.target.value === 'general_scan') setEventDate(''); }}>
                  <option value="general_scan">General River Change Scan</option>
                  <option value="event_based">Event-Based Analysis</option>
                </select>
                <small>{analysisMode === 'general_scan'
                  ? 'No event date is assumed. The earliest compatible observation is the baseline and later real acquisitions are compared in date order.'
                  : 'Supply an event date from an independent record. RiverShift never estimates or infers this date.'}</small>
              </label>
              {analysisMode === 'event_based' && <label className="rs-field"><span>Event date · independent record required</span><input type="date" value={eventDate} onChange={(event) => setEventDate(event.target.value)} min={startDate} max={endDate} required />
                <small>Event comparison needs at least one compatible acquisition strictly before this date and three strictly after it. A same-calendar-day acquisition is not assumed to be post-event.</small>
              </label>}
              <details className="rs-advanced-settings">
                <summary>Advanced settings</summary>
                <div className="rs-fields-two">
              <label className="rs-field"><span>Processing maturity</span>
                <select value={maturity} onChange={(event) => setMaturity(event.target.value as typeof maturity)}>
                  <option value="PROVISIONAL">PROVISIONAL</option><option value="BETA">BETA</option><option value="ALL">ALL (audit only)</option>
                </select>
              </label>
              <label className="rs-field"><span>Main-band polarization</span>
                <select value={polarization} onChange={(event) => setPolarization(event.target.value as typeof polarization)}>
                  <option value="">Any</option><option value="VV">VV</option><option value="VV+VH">VV + VH</option>
                  <option value="HH">HH</option><option value="HH+HV">HH + HV</option>
                  <option value="HH+HV+VH+VV">Full quad-pol</option><option value="LH+LV">LH + LV</option><option value="RH+RV">RH + RV</option>
                </select>
              </label>
              <label className="rs-field rs-field-full"><span>Orbit direction</span>
                <select value={direction} onChange={(event) => setDirection(event.target.value as typeof direction)}>
                  <option value="">Any direction</option><option value="ASCENDING">Ascending</option><option value="DESCENDING">Descending</option>
                </select>
              </label>
              <label className="rs-field rs-field-full"><span>NISAR frame coverage</span>
                <select value={frameCoverage} onChange={(event) => setFrameCoverage(event.target.value as typeof frameCoverage)}>
                  <option value="ALL">All footprints (recommended for discovery)</option>
                  <option value="FULL">Full frames only</option>
                  <option value="PARTIAL">Partial frames only</option>
                </select>
                <small>FULL/PARTIAL refers to NISAR frame coverage, not the percentage of your AOI covered. That must be checked separately.</small>
              </label>
              <label className="rs-field"><span>Science frequency</span>
                <select value={scienceFrequency} onChange={(event) => setScienceFrequency(event.target.value as typeof scienceFrequency)}>
                  <option value="frequencyA">Frequency A</option><option value="frequencyB">Frequency B</option>
                </select>
              </label>
              <label className="rs-field"><span>Diagonal covariance term</span>
                <select value={covarianceTerm} onChange={(event) => setCovarianceTerm(event.target.value)}>
                  <option>HHHH</option><option>HVHV</option><option>VVVV</option><option>VHVH</option><option>RHRH</option><option>RVRV</option>
                </select>
              </label>
              <label className="rs-field"><span>Common water threshold method</span>
                <select value={thresholdMethod} onChange={(event) => setThresholdMethod(event.target.value as typeof thresholdMethod)}>
                  <option value="otsu">Common Otsu · exploratory</option><option value="manual">Manual dB threshold</option>
                </select>
              </label>
              {thresholdMethod === 'manual' && <label className="rs-field"><span>Common threshold (dB)</span><input type="number" min="-60" max="0" step="0.1" required value={thresholdDb} onChange={(event) => setThresholdDb(event.target.value)} /></label>}
                </div>
              </details>
            </div>
            <button className="rs-primary-button" type="submit" disabled={riverJobBusy || !user || !aoiWkt.trim() || !startDate || !endDate || maturity === 'ALL' || (analysisMode === 'event_based' && !eventDate)}>
              {riverJobBusy ? 'Discovering, selecting and processing…' : 'Run RiverShift'}
            </button>
            <button className="rs-secondary-button" type="button" disabled={catalogueBusy || !aoiWkt.trim() || !startDate || !endDate || maturity === 'ALL'} onClick={() => void searchCatalogue()}>
              {catalogueBusy ? 'Searching ASF…' : 'Preview ASF catalogue (optional)'}
            </button>
            {!user && <p className="rs-muted">Sign in before running. Job status and private results remain owner-protected.</p>}
            {analysisMode === 'event_based' && eventDate && <p className="rs-warning-inline">Event date supplied by the user; it is not independently confirmed. Results remain provisional.</p>}
            {catalogueError && <p className="rs-error" role="alert">{catalogueError}</p>}
          </form>
          {catalogue && (
            <div className="rs-catalogue-result">
              <div className="rs-result-summary"><strong>{catalogue.product_count} product(s)</strong><span>{catalogue.message}</span></div>
              <p className="rs-muted">These are catalogue candidates, not individually selected processing inputs. RiverShift rechecks the same filters and then selects only dates/scenes that pass orbit, mode, HDF5-layer and AOI-footprint compatibility checks.</p>
              <div className="rs-info-banner">Catalogue discovery only · Data downloaded: No · Scientific analysis: Not performed</div>
              {catalogue.warnings.map((warning, index) => <p className="rs-warning-inline" key={index}>{warning}</p>)}
              {catalogue.products.length > 0 && (
                <div className="rs-table-wrap">
                  <table className="rs-table">
                    <thead><tr><th>Acquisition</th><th>Track / frame</th><th>Mode / pol.</th><th>Coverage</th><th>Maturity</th><th>Product</th></tr></thead>
                    <tbody>
                      {catalogue.products.map((product) => (
                        <tr key={product.granule_name}>
                          <td>{formatDate(product.start_time_utc)}</td>
                          <td>{product.flight_direction || '—'}<small>R{product.relative_orbit || '—'} · F{product.frame || '—'}</small></td>
                          <td>{product.bandwidth_mode || '—'}<small>{product.main_band_polarization || product.polarization_code || '—'}</small></td>
                          <td>{product.frame_coverage || 'Unknown'}</td>
                          <td><span className={'rs-maturity rs-' + product.maturity.toLowerCase()}>{product.maturity}</span></td>
                          <td>{product.download_url
                            ? <a href={product.download_url} target="_blank" rel="noreferrer">Open ASF product ↗</a>
                            : <span>URL unavailable</span>}
                            <small>{product.processing_type || 'Processing type unknown'} · {product.crid || 'CRID unknown'}</small><small>{product.granule_name}</small>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>

        <aside className="rs-panel rs-pipeline-panel">
          <p className="rs-kicker">02 · SCIENTIFIC WORKFLOW</p>
          <h2>From radar to evidence</h2>
          <ol className="rs-pipeline">
            <li><b>Discover</b><span>Find actual NISAR GCOV products in ASF DAAC.</span></li>
            <li><b>Prepare</b><span>Inspect HDF5, check maturity, extract the same diagonal term and clip the same AOI.</span></li>
            <li><b>Detect</b><span>Screen open water on comparable pre-event, flood-period and post-event observations.</span></li>
            <li><b>Measure</b><span>Quantify water coverage and persistent land/water transitions.</span></li>
            <li><b>Validate</b><span>Compare against an independent reference and report F1/IoU.</span></li>
          </ol>
          <a className="rs-doc-link" href="https://nisar-docs.asf.alaska.edu/gcov/" target="_blank" rel="noreferrer">NISAR GCOV product guide ↗</a>
          <a className="rs-doc-link" href="https://docs.asf.alaska.edu/api/keywords/" target="_blank" rel="noreferrer">ASF Search API documentation ↗</a>
          <a className="rs-doc-link" href="https://nisar-docs.asf.alaska.edu/availability-overview/" target="_blank" rel="noreferrer">NISAR data availability & maturity ↗</a>
        </aside>
      </section>

      {riverJobBusy && (
        <section className="rs-panel rs-job-feedback" role="status" aria-live="polite">
          <div className="rs-info-banner">
            <strong>Submitting RiverShift job…</strong>{' '}
            The API is validating the AOI and confirming the NASA CMR acquisition manifest before dispatch.
            No NISAR product download starts until this step succeeds.
          </div>
        </section>
      )}
      {riverJobError && riverJobError !== activeJobPreviewError && (
        <section className="rs-panel rs-job-feedback" aria-live="assertive">
          <p className="rs-error" role="alert">{riverJobError}</p>
        </section>
      )}
      {riverJob && <section className="rs-panel rs-job-panel" aria-live="polite">
        <div className="rs-section-heading"><div><p className="rs-kicker">LIVE SCIENCE JOB</p><h2>{riverJob.state.replace(/_/g, ' ')} · {riverJob.stage.replace(/_/g, ' ')}</h2></div>
          <span className="rs-chip">{riverJob.maturity} · {riverJob.frequency} / {riverJob.covariance_term}</span></div>
        <p className="rs-muted">{riverJob.analysis_mode === 'general_scan' ? 'General River Change Scan' : 'Event-Based Analysis'} · Job ID {riverJob.id} · {riverJob.start_date} to {riverJob.end_date}{riverJob.event_date ? ` · Event date ${riverJob.event_date}` : ''}</p>
        {riverJob.github_run_url && <p><a href={riverJob.github_run_url} target="_blank" rel="noreferrer">Open processing run logs ↗</a></p>}
        {riverJob.error_message && <p className="rs-error" role="alert">{riverJob.error_message}</p>}
        {riverJob.state === 'succeeded' && <div className="rs-warning-inline"><strong>Processing complete · PROVISIONAL.</strong> {riverJob.analysis_mode === 'general_scan'
          ? 'No event date was supplied or inferred. Independent scientific validation is still pending.'
          : 'The event date was user supplied and has not been independently confirmed; independent scientific validation is still pending.'}
          {' '}The map below can show a checksum-checked display preview; its measurements are not verified.</div>}
        {riverJob.state === 'succeeded' && riverJob.analysis_mode === 'event_based' && eventTiming && (
          <div className="rs-warning-inline" role="status">
            <strong>Event-to-observation gap: +{eventTiming.offsetDays} days.</strong>
            {' '}The first qualifying NISAR acquisition was {eventTiming.observationDate}, after the supplied event date {eventTiming.eventDate}.
            This is an actual acquisition, not a measured flood-peak stage. Check a same-stage gauge or independent reference before attributing mapped water or morphology changes to this event.
          </div>
        )}
        {riverJob.state === 'succeeded' && riverJob.result_summary && (
          <OverlapAuditPanel summary={riverJob.result_summary} />
        )}
        {jobPreview?.jobId === riverJob.id && jobPreview.report && jobTimeSeriesFrames.length > 0 && (
          <section className="rs-timeseries" aria-label="Provisional observed acquisition playback">
            <div className="rs-section-heading"><div><p className="rs-kicker">OBSERVED ACQUISITIONS</p><h3>River change playback</h3></div>
              <span className="rs-chip">PROVISIONAL · NO DAILY INTERPOLATION</span></div>
            <p className="rs-warning-inline">Each step is an actual NISAR acquisition date. The blue layer shows open-water candidates; the red and green layers show persistent erosion and deposition candidates. These are radar screening results, not field-confirmed change.</p>
            <div className="rs-playback-controls">
              <button className="rs-secondary-button" type="button" onClick={() => setJobTimeSeriesIndex((index) => (index - 1 + jobTimeSeriesFrames.length) % jobTimeSeriesFrames.length)}>Previous</button>
              <button className="rs-primary-button" type="button" disabled={jobTimeSeriesFrames.length < 2} onClick={() => setJobTimeSeriesPlaying((value) => !value)}>{jobTimeSeriesPlaying ? 'Pause' : 'Play'}</button>
              <button className="rs-secondary-button" type="button" onClick={() => setJobTimeSeriesIndex((index) => (index + 1) % jobTimeSeriesFrames.length)}>Next</button>
              <label className="rs-date-slider"><span>Observed date</span>
                <input type="range" min={0} max={jobTimeSeriesFrames.length - 1} value={jobTimeSeriesIndex}
                  onChange={(event) => { setJobTimeSeriesPlaying(false); setJobTimeSeriesIndex(Number(event.target.value)); }} />
              </label>
            </div>
            {riverJob.analysis_mode === 'general_scan' && (
              <div className="rs-marker-control">
                <label className="rs-marker-toggle">
                  <input
                    type="checkbox"
                    checked={showGeneralChangeMarkers}
                    disabled={generalChangeMarkers.length === 0}
                    onChange={(event) => setShowGeneralChangeMarkers(event.target.checked)}
                  />
                  <span>
                    <strong>Show change-candidate markers during playback</strong>
                    <small>Markers use the retained erosion, deposition and new-water candidate footprints.</small>
                  </span>
                </label>
                <p className="rs-marker-note">{generalChangeMarkers.length > 0
                  ? `${generalChangeMarkers.length} representative marker(s) shown (up to 24 per class). They remain visible across observed-date steps; they are window-level candidates, not date-specific measurements.`
                  : 'No erosion, deposition or new-water candidate footprints are available for markers in this result.'}</p>
              </div>
            )}
            {riverJob.analysis_mode === 'general_scan' && (
              <>
                <div className="rs-change-legend" aria-label="General Scan candidate map legend">
                  <span><i className="rs-change-swatch rs-change-swatch-erosion" aria-hidden="true" />Erosion candidate</span>
                  <span><i className="rs-change-swatch rs-change-swatch-deposition" aria-hidden="true" />Deposition candidate</span>
                  <span><i className="rs-change-swatch rs-change-swatch-new-water" aria-hidden="true" />New-water candidate</span>
                </div>
                <p className="rs-change-legend-note">Translucent areas show the retained date-window candidate footprints. The AOI remains outline-only; candidate colours do not imply field confirmation.</p>
              </>
            )}
            {currentJobFrameMetadata && <p className="rs-info-banner">NISAR observation {currentJobFrameMetadata.date} · frame {jobTimeSeriesIndex + 1}/{jobTimeSeriesFrames.length} · open-water candidate area {currentJobFrameMetadata.water_area_km2.toFixed(3)} km² · valid common-support pixels {(currentJobFrameMetadata.valid_pixel_fraction * 100).toFixed(1)}% · source {currentJobFrameMetadata.granule_name || 'granule ID recorded in source report'}{!currentJobFrame ? ' · loading checksum-verified map layer…' : ''}</p>}
          </section>
        )}
        {jobPreview?.jobId === riverJob.id && jobPreview.error && <p className="rs-warning-inline" role="status">{jobPreview.error}</p>}
        {!!riverJob.artifact_manifest?.length && <div className="rs-artifact-list"><strong>Private result files · upload checksum verified</strong><p className="rs-muted">Upload receipts preserve the original size and checksum. In-app downloads and previews are checked again before use.</p>{riverJob.artifact_manifest.map((artifact, index) => artifact.bytes <= 4_000_000
          ? <button className="rs-secondary-button" key={`${artifact.path}-${index}`} type="button" onClick={() => void saveRiverArtifact(index, artifact.name)}>{artifact.name} · {(artifact.bytes / 1_000_000).toFixed(2)} MB</button>
          : <span className="rs-muted" key={`${artifact.path}-${index}`}>{artifact.name} · {(artifact.bytes / 1_000_000).toFixed(2)} MB · stored privately; too large for in-app download</span>)}</div>}
      </section>}

      <section className="rs-panel rs-results-panel">
        <div className="rs-section-heading">
          <div><p className="rs-kicker">03 · RESULTS EXPLORER</p><h2>Inspect reproducible outputs</h2></div>
          <span className="rs-chip">NO SYNTHETIC METRICS</span>
        </div>
        <p className="rs-muted">
          Live RiverShift jobs load their checksum-checked report and map previews here automatically.
          The import controls below are optional and are intended for external or offline research results.
          Catalogue rows are never presented as measured flood or erosion results.
        </p>
        <p className="rs-kicker">OPTIONAL MANUAL / RESEARCH IMPORT</p>
        <div className="rs-upload-grid">
          <label className="rs-upload"><span>Import result JSON</span><small>Flood, morphology or validation report</small>
            <input type="file" accept=".json,application/json" onChange={(event) => void loadReport(event.target.files?.[0])} />
          </label>
          <label className="rs-upload"><span>Import change GeoJSON layer(s)</span><small>For playback, select every `water_YYYY-MM-DD.geojson` named in the report</small>
            <input type="file" multiple accept=".geojson,.json,application/geo+json,application/json" onChange={(event) => void loadGeoJSONFiles(event.target.files || undefined)} />
          </label>
        </div>
        {reportError && <p className="rs-error" role="alert">{reportError}</p>}
        {geoJSONError && <p className="rs-error" role="alert">{geoJSONError}</p>}
        {explorerReport && <ReportPanel report={explorerReport} />}
        {report?.analysis_type === 'water_timeseries_screening' && (
          <section className="rs-timeseries" aria-label="Observed acquisition playback">
            <div className="rs-section-heading"><div><p className="rs-kicker">OBSERVED ACQUISITIONS</p><h3>Water change playback</h3></div>
              <span className="rs-chip">NO DAILY INTERPOLATION</span></div>
            {report.quality.release_status !== 'VERIFIED'
              ? <p className="rs-warning-inline">Playback and map layers stay hidden until the report is independently reviewed and marked VERIFIED.</p>
              : playbackFrames.length !== (report.time_series || []).length
                ? <p className="rs-warning-inline">Import every report-listed GeoJSON file in one selection. Each filename and SHA-256 must match before playback starts.</p>
                : playbackFrames.length < 2
                  ? <p className="rs-warning-inline">At least two verified observation frames are required to play this time series.</p>
                  : <>
                    <div className="rs-playback-controls">
                      <button className="rs-secondary-button" type="button" onClick={() => setTimeSeriesIndex((index) => (index - 1 + playbackFrames.length) % playbackFrames.length)}>Previous</button>
                      <button className="rs-primary-button" type="button" onClick={() => setTimeSeriesPlaying((value) => !value)}>{timeSeriesPlaying ? 'Pause' : 'Play'}</button>
                      <button className="rs-secondary-button" type="button" onClick={() => setTimeSeriesIndex((index) => (index + 1) % playbackFrames.length)}>Next</button>
                      <label className="rs-date-slider"><span>Acquisition date</span>
                        <input type="range" min={0} max={playbackFrames.length - 1} value={timeSeriesIndex}
                          onChange={(event) => { setTimeSeriesPlaying(false); setTimeSeriesIndex(Number(event.target.value)); }} />
                      </label>
                    </div>
                    {currentTimeSeriesFrame && <p className="rs-info-banner">Observed {currentTimeSeriesFrame.date} · candidate open-water area {currentTimeSeriesFrame.water_area_km2.toFixed(3)} km² · valid samples {(currentTimeSeriesFrame.valid_pixel_fraction * 100).toFixed(1)}%</p>}
                  </>}
          </section>
        )}
        <section className="rs-map-section">
          <div className="rs-section-heading"><div><p className="rs-kicker">GEOSPATIAL OUTPUT</p><h3>Change map</h3></div></div>
          {mapError && <p className="rs-warning-inline">{mapError}</p>}
          <div className="rs-map" ref={resultMapElement} role="img" aria-label="Google satellite basemap with checksum-checked RiverShift result overlays" />
          <div className="rs-legend"><span><i className="rs-swatch rs-erosion" />Erosion candidate</span><span><i className="rs-swatch rs-deposition" />Deposition candidate</span><span><i className="rs-swatch rs-water" />New-water candidate</span><span><i className="rs-swatch rs-observed-water" />Observed-date water extent</span></div>
          {(hasCandidateOverlaySource || hasObservedWaterFrames) && (
            <div className="rs-overlay-controls" aria-label="RiverShift map overlay visibility">
              {hasCandidateOverlaySource && (
                <>
                  <label className="rs-overlay-toggle rs-overlay-toggle-all">
                    <input
                      type="checkbox"
                      checked={allCandidateLayersVisible}
                      onChange={(event) => {
                        setShowErosionOverlay(event.target.checked);
                        setShowDepositionOverlay(event.target.checked);
                        setShowNewWaterOverlay(event.target.checked);
                      }}
                    />
                    <span><strong>All candidate overlays</strong><small>Show or hide all implemented change classes.</small></span>
                  </label>
                  <div className="rs-overlay-class-grid">
                    <label className="rs-overlay-toggle">
                      <input type="checkbox" checked={showErosionOverlay} disabled={candidateOverlayCounts.erosion === 0}
                        onChange={(event) => setShowErosionOverlay(event.target.checked)} />
                      <span><strong>Erosion</strong><small>{candidateOverlayCounts.erosion} feature(s)</small></span>
                    </label>
                    <label className="rs-overlay-toggle">
                      <input type="checkbox" checked={showDepositionOverlay} disabled={candidateOverlayCounts.deposition === 0}
                        onChange={(event) => setShowDepositionOverlay(event.target.checked)} />
                      <span><strong>Deposition</strong><small>{candidateOverlayCounts.deposition} feature(s)</small></span>
                    </label>
                    <label className="rs-overlay-toggle">
                      <input type="checkbox" checked={showNewWaterOverlay} disabled={candidateOverlayCounts.newWater === 0}
                        onChange={(event) => setShowNewWaterOverlay(event.target.checked)} />
                      <span><strong>New water</strong><small>{candidateOverlayCounts.newWater} feature(s)</small></span>
                    </label>
                    <label className="rs-overlay-toggle rs-overlay-unavailable">
                      <input type="checkbox" disabled />
                      <span><strong>Exposed bar / char</strong><small>Unavailable — detection method not yet validated or implemented.</small></span>
                    </label>
                  </div>
                </>
              )}
              {hasObservedWaterFrames && (
                <label className="rs-overlay-toggle rs-overlay-observed">
                  <input type="checkbox" checked={showObservedWaterExtent}
                    onChange={(event) => setShowObservedWaterExtent(event.target.checked)} />
                  <span><strong>Observed-date open-water extent</strong><small>Current acquired-date water layer; separate from date-window change candidates.</small></span>
                </label>
              )}
              <p className="rs-overlay-note">Visibility controls affect the map only. Full-resolution masks and scientific metrics are unchanged. Preview geometry is display-only and may generalise or visually overlap separate classes.</p>
            </div>
          )}
          <div className="rs-data-sources" aria-label="RiverShift data sources and roles">
            <div><strong>NASA measurement data</strong><span>NASA–ISRO NISAR Level-2 GCOV radar observations drive the water and land-transition screening.</span></div>
            <div><strong>NASA data access</strong><span>NASA Earthdata and ASF DAAC provide the exact NISAR granules; product identities and checksums are retained with the result.</span></div>
            <div><strong>Third-party map context only</strong><span>Google Maps Satellite imagery helps orient the result geographically. Basemap imagery is not used by the analysis and is not an independent validation layer.</span></div>
            <div><strong>Optional independent agency comparison</strong><span>Copernicus Sentinel-2 L2A can be compared with a supplied NISAR water mask using clear-sky SCL and B03/B11 MNDWI. This optional screen is not run for this result unless a comparison report is attached, and it is not ground truth.</span></div>
          </div>
          {hasVerifiedOverlay && <p className="rs-muted">Verified layer(s) are drawn; each GeoJSON SHA-256 matches the loaded report. Candidate classes require scientific interpretation and are not field-confirmed land change.</p>}
          {hasJobPreviewOverlay && !hasVerifiedOverlay && <p className="rs-warning-inline">Provisional map preview is derived from checksum-bound full-resolution masks using lower-resolution occupancy footprints. Different classes can appear to overlap at preview scale even when their full-resolution masks do not. The map is for visual orientation only; full-resolution masks and measurements remain authoritative and PROVISIONAL.</p>}
          {report && geoJSONFiles.length > 0 && !hasVerifiedOverlay && <p className="rs-warning-inline" role="status">Loaded GeoJSON is not drawn: the report is not VERIFIED, the checksum does not match, or the complete time-series layer set is missing.</p>}
        </section>
        {!report && !hasLiveJobResult && <div className="rs-empty-state">
          <strong>{hasLiveJobResultPending ? 'Live processing finished · preview not loaded' : resultType}</strong>
          <p>{hasLiveJobResultPending
            ? 'A successful live job and its private result files are on record, but the automatic preview has not loaded. See the storage message above; manual import is not required for a live job.'
            : 'No live result or external report is loaded yet. Start the live processing workflow above; manual JSON and GeoJSON import is optional.'}</p>
        </div>}
        {hasLiveJobResult && !report && <p className="rs-info-banner" role="status">
          Live NISAR result loaded automatically from the completed job. No CLI run or manual file import is needed. The preview remains PROVISIONAL until independent validation is attached.
        </p>}
      </section>

      <footer className="rs-footer">
        <span>SARGuardian · RiverShift</span><span>Open science · Reproducible processing · Evidence before claims</span>
      </footer>
    </main>
  );
}
