import type { CatalogProduct, ProcessingJobState } from '../../src/api/catalog';

const activeStates = new Set<ProcessingJobState>(['queued', 'dispatching', 'running']);

export const ELAPSED_TICK_MS = 1000;
export type GranuleCounts = { GUNW: number; GOFF: number; source: 'catalog-preflight' | 'worker-discovery' | 'processed-results' };

export function countCatalogGranules(products: CatalogProduct[]): GranuleCounts {
  return {
    GUNW: products.filter((product) => product.product_type === 'GUNW').length,
    GOFF: products.filter((product) => product.product_type === 'GOFF').length,
    source: 'catalog-preflight',
  };
}

export function formatElapsed(createdAt: string, updatedAt: string, state: ProcessingJobState, now: number) {
  const finishedAt = activeStates.has(state) ? now : new Date(updatedAt).getTime();
  const milliseconds = Math.max(0, finishedAt - new Date(createdAt).getTime());
  const seconds = Math.floor(milliseconds / 1000);
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}
