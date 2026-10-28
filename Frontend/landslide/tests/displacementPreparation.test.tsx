import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { DisplacementJobStatus, DisplacementPreparation, JOB_STATUS_POLL_MS } from '../ui/DisplacementPreparation';
import { chooseAccountJob } from '../engine/displacementJobSelection';
import { ScientificResults } from '../ui/ScientificResults';
import { countCatalogGranules, ELAPSED_TICK_MS, formatElapsed } from '../ui/processingStatus';
import { prepareInvestigation } from '../engine/investigation';
import { isLandslideDisplacementRoute, LANDSLIDE_DISPLACEMENT_PATH, LANDSLIDE_PATH, LANDSLIDE_RESULTS_PATH, navigatePath } from '../../src/routing';
import { DEFAULT_DISPLACEMENT_OPTIONS } from '../../src/api/catalog';
import { waitForDashboardPopupLoad, writeDashboardHtml } from '../ui/dashboardPopup';

describe('dashboard opens in a new same-origin tab', () => {
  it('offers the new-tab dashboard action only for a verified successful result', () => {
    const job = {
      id: '736bd4e6-8963-43be-a916-bc62336c8557',
      state: 'succeeded',
      result_summary: {
        web_dashboard: { status: 'PASS' },
        artifacts: [{ path: 'aoi-derived-results.zip' }],
      },
    } as never;
    const html = renderToStaticMarkup(<ScientificResults job={job} user={{ getIdToken: vi.fn() } as never} />);
    expect(html).toContain('Open interactive dashboard');
    expect(html).not.toContain('Load dashboard in Processing');
    expect(html).not.toContain('<iframe');
  });
});

describe('same-origin dashboard popup helper', () => {
  it('waits until the popup has loaded an actual site-origin document', async () => {
    let readyState = 'loading';
    let loadHandler: (() => void) | undefined;
    const origin = 'https://sar-guardian-dynamic-lilac.vercel.app';
    const document = {
      get URL() { return `${origin}/`; },
      get readyState() { return readyState; },
    };
    const addEventListener = vi.fn((_type: string, handler: EventListenerOrEventListenerObject) => {
      loadHandler = typeof handler === 'function' ? () => handler(new Event('load')) : undefined;
    });
    const removeEventListener = vi.fn();
    const popup = { document, addEventListener, removeEventListener } as unknown as Window;

    const ready = waitForDashboardPopupLoad(popup, origin);
    expect(removeEventListener).not.toHaveBeenCalled();
    readyState = 'complete';
    loadHandler?.();
    await ready;

    expect(removeEventListener).toHaveBeenCalledOnce();
    expect(removeEventListener).toHaveBeenCalledWith('load', expect.any(Function));
  });

  it('writes only the verified dashboard HTML into the popup document', () => {
    const document = { open: vi.fn(), write: vi.fn(), close: vi.fn() };
    writeDashboardHtml({ document } as unknown as Window, '<!doctype html><html><body>verified</body></html>');
    expect(document.open).toHaveBeenCalledOnce();
    expect(document.write).toHaveBeenCalledWith('<!doctype html><html><body>verified</body></html>');
    expect(document.close).toHaveBeenCalledOnce();
  });
});

describe('displacement preparation route', () => {
  const user = { getIdToken: vi.fn() } as never;
  it('keeps the AOI payload out of the processing screen', () => {
    const preparation = prepareInvestigation({ coordinates: [[85, 27], [85.001, 27], [85, 27.001]], areaClosed: true, startDate: '2026-10-01', endDate: '2026-10-22', today: '2026-10-23' });
    const html = renderToStaticMarkup(<DisplacementPreparation preparation={preparation} onBack={() => {}} user={user} />);
    expect(html).not.toContain('POLYGON');
    expect(html).not.toContain('Copy WKT');
    expect(html).not.toContain('Copy Payload');
    expect(html).not.toContain('Start displacement processing');
    expect(html).not.toContain('Back to map');
  });
  it('does not render a second submission screen on an empty/direct visit', () => {
    const html = renderToStaticMarkup(<DisplacementPreparation preparation={{ valid: false, errors: ['Select an AOI.'] }} onBack={() => {}} user={user} />);
    expect(html).toBe('');
    expect(html).not.toContain('POLYGON');
    expect(html).not.toContain('Copy Payload');
  });
  it('does not reuse a completed displacement result for another selected place or target', () => {
    const coordinates = [[85, 27], [85.001, 27], [85, 27.001]] as const;
    const preparation = prepareInvestigation({
      coordinates: [...coordinates], areaClosed: true, startDate: '2026-10-01',
      endDate: '2026-10-22', today: '2026-10-23',
      place: { name: 'Test location', latitude: 27.0002, longitude: 85.0002 },
    });
    expect(preparation.valid).toBe(true);
    if (!preparation.valid) return;
    const base = {
      state: 'succeeded', stage: 'completed', aoi_wkt: preparation.payload.aoi_wkt,
      start_date: '2026-10-01', end_date: '2026-10-22',
      product_type: 'BOTH', acquisition_window: 'secondary-acquisition-in-window',
      retention_mode: 'derived-only', processing_profile: 'full-aoi',
      created_at: '2026-10-23T10:00:00Z', updated_at: '2026-10-23T10:10:00Z',
    };
    const oldPlace = {
      ...base, id: 'old-place-job', place_name: 'Nearby village',
      place_latitude: 27.0003, place_longitude: 85.0003,
      target_latitude: 27.0003, target_longitude: 85.0003,
    };
    const matching = {
      ...base, id: 'matching-place-job', place_name: 'Test location',
      place_latitude: 27.0002, place_longitude: 85.0002,
      target_latitude: 27.0002, target_longitude: 85.0002,
    };
    const options = DEFAULT_DISPLACEMENT_OPTIONS;
    expect(chooseAccountJob([oldPlace, matching] as never, preparation, options)?.id).toBe('matching-place-job');
    expect(chooseAccountJob([oldPlace] as never, preparation, options)).toBeNull();
  });

  it('uses existing navigation for both workflows and back, suppressing duplicate pushes', () => {
    const push = vi.fn();
    let path = navigatePath(LANDSLIDE_RESULTS_PATH, LANDSLIDE_PATH, push);
    expect(path).toBe('/landslide/results');
    path = navigatePath(LANDSLIDE_PATH, path, push);
    path = navigatePath(LANDSLIDE_DISPLACEMENT_PATH, path, push);
    expect(isLandslideDisplacementRoute(path)).toBe(true);
    navigatePath(path, path, push);
    expect(push).toHaveBeenCalledTimes(3);
    expect(navigatePath(LANDSLIDE_PATH, path, push)).toBe('/landslide');
  });
  it('does not expose a second submission control on the task status route', () => {
    const preparation = prepareInvestigation({ coordinates: [[85, 27], [85.001, 27], [85, 27.001]], areaClosed: true, startDate: '2026-10-01', endDate: '2026-10-22', today: '2026-10-23' });
    const html = renderToStaticMarkup(<DisplacementPreparation preparation={preparation} onBack={() => {}} user={user} />);
    expect(html).not.toContain('Start displacement processing');
    expect(html).not.toContain('Preparing displacement job');
  });
  it('keeps a one-second age tick and renders failed recovery actions with granule counts', () => {
    expect(ELAPSED_TICK_MS).toBe(1000);
    expect(JOB_STATUS_POLL_MS).toBe(2000);
    expect(DEFAULT_DISPLACEMENT_OPTIONS).toEqual({
      product_type: 'BOTH', acquisition_window: 'secondary-acquisition-in-window',
      retention_mode: 'derived-only', processing_profile: 'full-aoi',
    });
    expect(formatElapsed('2026-10-24T10:00:00Z', '2026-10-24T10:00:02Z', 'running', Date.parse('2026-10-24T10:00:03Z'))).toBe('0m 3s');
    expect(countCatalogGranules([
      { product_type: 'GUNW' }, { product_type: 'GOFF' }, { product_type: 'GUNW' },
    ] as never)).toEqual({ GUNW: 2, GOFF: 1, source: 'catalog-preflight' });
    const failedJob = {
      id: 'failed-job', state: 'failed', stage: 'failed', aoi_wkt: 'POLYGON((85 27, 85.01 27, 85 27.01, 85 27))',
      start_date: '2026-09-27', end_date: '2026-10-24', created_at: '2026-10-24T10:00:00Z', updated_at: '2026-10-24T10:01:05Z',
      error_message: 'Worker failed.',
    } as const;
    const html = renderToStaticMarkup(<DisplacementJobStatus job={failedJob} pendingAction={null} clock={Date.parse('2026-10-24T10:02:00Z')}
      granuleCounts={{ GUNW: 4, GOFF: 3, source: 'catalog-preflight' }} onCancel={() => {}} onRefresh={() => {}}
      onRetry={() => {}} onBack={() => {}} />);
    expect(html).toContain('Age:');
    expect(html).toContain('1m 5s');
    expect(html).toContain('GUNW granules');
    expect(html).toContain('>4<');
    expect(html).toContain('GOFF granules');
    expect(html).toContain('>3<');
    expect(html).toContain('Retry task');
    expect(html).toContain('Go to map');
    expect(html).toContain('Selected place:');
    expect(html).toContain('Measurement target:');
    expect(html).toContain('Not supplied inside the submitted AOI');
    expect(html).not.toContain('Start displacement processing');

    const targetedJob = {
      ...failedJob, place_name: 'Rasuwa foothill', place_latitude: 27.01, place_longitude: 85.01,
      target_latitude: 27.01, target_longitude: 85.01,
    } as const;
    const targetedHtml = renderToStaticMarkup(<DisplacementJobStatus job={targetedJob as never} pendingAction={null}
      clock={Date.parse('2026-10-24T10:02:00Z')} granuleCounts={null} onCancel={() => {}} onRefresh={() => {}}
      onRetry={() => {}} onBack={() => {}} />);
    expect(targetedHtml).toContain('Rasuwa foothill');
    expect(targetedHtml).toContain('27.01000, 85.01000');
    expect(targetedHtml).toContain('not a stable-ground reference');

    const runningWithWorkerCounts = { ...failedJob, state: 'running', stage: 'products_discovered',
      github_run_url: 'https://github.com/shourov-04/SARGuardian_Dynamic/actions/runs/123', result_summary: {
      catalog_granule_counts: { GUNW: 8, GOFF: 5 }, selected_granule_counts: { GUNW: 1, GOFF: 1 },
    } } as const;
    const runningHtml = renderToStaticMarkup(<DisplacementJobStatus job={runningWithWorkerCounts as never} pendingAction={null}
      clock={Date.parse('2026-10-24T10:02:00Z')} granuleCounts={null} onCancel={() => {}} onRefresh={() => {}}
      onRetry={() => {}} onBack={() => {}} />);
    expect(runningHtml).toContain('>8<');
    expect(runningHtml).toContain('>5<');
    expect(runningHtml).toContain('Worker discovery');
    expect(runningHtml).toContain('Refresh task');
    expect(runningHtml).not.toContain('View worker logs');
    expect(runningHtml).not.toContain('https://github.com/shourov-04/SARGuardian_Dynamic/actions/runs/123');

    const refreshingHtml = renderToStaticMarkup(<DisplacementJobStatus job={runningWithWorkerCounts as never} pendingAction="refresh"
      clock={Date.parse('2026-10-24T10:02:00Z')} granuleCounts={null} onCancel={() => {}} onRefresh={() => {}}
      onRetry={() => {}} onBack={() => {}} />);
    expect(refreshingHtml).toContain('Refreshing…');
    expect(refreshingHtml).toContain('Cancel task');
    expect(refreshingHtml).not.toContain('Cancelling…');
  });

  it('shows only one Drive-folder download action after successful processing', () => {
    const succeededJob = {
      id: 'succeeded-job', state: 'succeeded', stage: 'completed',
      aoi_wkt: 'POLYGON((85 27, 85.01 27, 85 27.01, 85 27))',
      start_date: '2026-09-27', end_date: '2026-10-24',
      created_at: '2026-10-24T10:00:00Z', updated_at: '2026-10-24T10:10:00Z',
      result_summary: {
        storage_verification: { status: 'PASS', remote_folder_id: 'private-drive-folder-123' },
        diagnostics: { pixel_stack: { status: 'PASS' } },
        products: [{ product_type: 'GUNW' }],
        artifacts: [{ path: 'aoi-derived-results.zip' }],
      },
    } as const;

    const html = renderToStaticMarkup(<ScientificResults job={succeededJob as never} />);

    expect(html).toContain('Download results');
    expect(html).toContain('https://drive.google.com/drive/folders/private-drive-folder-123');
    expect(html).not.toContain('Scientific diagnostics');
    expect(html).not.toContain('Input products');
    expect(html).not.toContain('Retained scientific outputs');
  });
});
