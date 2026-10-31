import type { Engine } from '../data/engineData';
import type { CSSProperties } from 'react';

type EngineCardProps = {
  engine: Engine;
  onSelect: (engine: Engine) => void;
};

function EngineIcon({ type }: { type: Engine['icon'] }) {
  const common = {
    fill: 'none',
    stroke: 'currentColor',
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    strokeWidth: 1.8,
  };

  if (type === 'landslide') {
    return (
      <svg viewBox="0 0 48 48" aria-hidden="true" {...common}>
        <path d="M4 38 19 13l9 14 5-8 11 19H4Z" />
        <path d="m18 30 5-5 5 5m-7-1 2 7m8-7 3 5" />
      </svg>
    );
  }

  if (type === 'glacier') {
    return (
      <svg viewBox="0 0 48 48" aria-hidden="true" {...common}>
        <path d="M5 37 18 15l8 12 6-9 11 19H5Z" />
        <path d="m18 15 4 7-4 4 6 5 2-5 5-4m-7 9 2 6" />
      </svg>
    );
  }

  if (type === 'known-place') {
    return (
      <svg viewBox="0 0 48 48" aria-hidden="true" {...common}>
        <path d="M24 42s14-13 14-24a14 14 0 1 0-28 0c0 11 14 24 14 24Z" />
        <circle cx="24" cy="18" r="4.5" />
        <path d="M7 42h34" />
      </svg>
    );
  }

  return (
    <svg viewBox="0 0 48 48" aria-hidden="true" {...common}>
      <path d="M4 15c6-6 12 6 20 0s13 6 20 0M4 24c6-6 12 6 20 0s13 6 20 0M4 33c6-6 12 6 20 0s13 6 20 0" />
    </svg>
  );
}

export function EngineCard({ engine, onSelect }: EngineCardProps) {
  return (
    <button
      className={`engine-card ${engine.status === 'AVAILABLE' ? 'is-available' : 'is-preview'}`}
      type="button"
      onClick={() => onSelect(engine)}
      style={{ '--engine-image': `url("${engine.image}")` } as CSSProperties}
      aria-label={`${engine.name}: ${engine.actionLabel}`}
    >
      <span className="engine-wash" aria-hidden="true" />
      <span className="engine-content">
        <span className="engine-icon"><EngineIcon type={engine.icon} /></span>
        <span className="engine-copy">
          <span className="engine-name">{engine.name}</span>
          <span className="engine-description">{engine.description}</span>
        </span>
        <span className="engine-action">
          <span className={`engine-status ${engine.status === 'AVAILABLE' ? 'active' : 'preview'}`}>{engine.status}</span>
          <span className="engine-action-label">{engine.actionLabel}</span>
          <span className="arrow" aria-hidden="true">→</span>
        </span>
      </span>
    </button>
  );
}
