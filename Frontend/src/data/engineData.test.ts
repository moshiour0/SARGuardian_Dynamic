import { describe, expect, it } from 'vitest';
import { engines } from './engineData';

describe('SARGuardian analysis entry points', () => {
  it('shows the four agreed homepage workflows in the agreed order', () => {
    expect(engines.map((engine) => engine.name)).toEqual([
      'Landslide',
      'Glacier Movement',
      'Known Place Analysis',
      'River Shift',
    ]);
  });

  it('keeps River Shift in research preview until its dedicated workflow is ready', () => {
    const river = engines.find((engine) => engine.id === 'river-shift');
    expect(river?.status).toBe('RESEARCH PREVIEW');
  });

  it('uses the shared AOI displacement workflow for glacier terrain without claiming a separate model', () => {
    const glacier = engines.find((engine) => engine.id === 'glacier');
    expect(glacier?.description).toContain('same AOI-wide displacement workflow');
    expect(glacier?.description.toLowerCase()).not.toContain('collapse prediction');
  });
});
