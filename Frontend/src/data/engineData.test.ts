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

  it('exposes River Shift as its own available engine entry', () => {
    const river = engines.find((engine) => engine.id === 'river-shift');
    expect(river?.status).toBe('RESEARCH PREVIEW');
    expect(river?.actionLabel).toBe('Explore global river change');
    expect(river?.description).toContain('worldwide');
    expect(river?.description).toContain('NISAR L-band GCOV');
  });

  it('uses the shared AOI displacement workflow for glacier terrain without claiming a separate model', () => {
    const glacier = engines.find((engine) => engine.id === 'glacier');
    expect(glacier?.description).toContain('same AOI-wide displacement workflow');
    expect(glacier?.description.toLowerCase()).not.toContain('collapse prediction');
  });
  
  it('provides secure image sources for every homepage workflow card', () => {
    expect(engines).toHaveLength(4);
    for (const engine of engines) {
      const imageUrl = new URL(engine.image);
      expect(imageUrl.protocol).toBe('https:');
      expect(imageUrl.hostname).toBe('images.unsplash.com');
    }
  });
});
