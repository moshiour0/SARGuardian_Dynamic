import { useState } from 'react';
import type { Engine } from '../data/engineData';
import { engines } from '../data/engineData';
import { EngineCard } from '../components/EngineCard';
import { Footer } from '../components/Footer';
import { Header } from '../components/Header';
import { Hero } from '../components/Hero';
import { HomeSections } from './HomeSections';

import type { User } from 'firebase/auth';

type HomeProps = {
  user: User | null;
  authState: 'idle' | 'loading' | 'ready' | 'error';
  authError: string | null;
  firebaseConfigured: boolean;
  missingFirebaseConfig: readonly string[];
  showAuthModal?: boolean;
  onCloseAuthModal?: () => void;
  onSignIn: () => void;
  onSignOut: () => void;
  onSelectEngine: (engine: Engine) => void;
  onOpenWorkspace: () => void;
};

export function Home({ user, authState, authError, firebaseConfigured, missingFirebaseConfig, showAuthModal = false, onCloseAuthModal, onSignIn, onSignOut, onSelectEngine, onOpenWorkspace }: HomeProps) {
  const [previewEngine, setPreviewEngine] = useState<Engine | null>(null);

  const handleEngineSelect = (engine: Engine) => {
    if (engine.id === 'river-shift') {
      setPreviewEngine(engine);
      return;
    }
    onSelectEngine(engine);
  };

  return (
    <div className="home-page">
      <div className="environment-layer" aria-hidden="true" />
      <div className="ambient-vignette" aria-hidden="true" />
      <Header user={user} authState={authState} onSignIn={onSignIn} onSignOut={onSignOut} />
      <main className="home-main">
        {!firebaseConfigured && (
          <section className="setup-banner" role="status">
            Local sign-in setup required. Missing: {missingFirebaseConfig.join(', ')}.
          </section>
        )}
        <Hero onStartExploring={onOpenWorkspace} />
        <section className="engine-section" id="analysis-tools" aria-labelledby="engines-title">
          <div className="engine-section-heading">
            <div>
              <p className="section-eyebrow">ANALYSIS WORKFLOWS</p>
              <h2 id="engines-title">Choose what you want to investigate</h2>
            </div>
            <p>Each workflow begins with an area and an observation window. The evidence shown depends on the data available for that selection.</p>
          </div>
          <div className="engine-grid">
            {engines.map((engine) => <EngineCard key={engine.id} engine={engine} onSelect={handleEngineSelect} />)}
          </div>
        </section>
        {authState === 'error' && <section className="error-banner" role="alert">{authError}</section>}
        <HomeSections user={user} onSignIn={onSignIn} onOpenWorkspace={onOpenWorkspace} />
      </main>
      {previewEngine && (
        <div className="auth-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="river-preview-title">
          <div className="auth-modal feature-preview-modal">
            <button className="auth-modal-close" type="button" onClick={() => setPreviewEngine(null)} aria-label="Close research preview">×</button>
            <p className="section-eyebrow">RESEARCH PREVIEW</p>
            <h2 id="river-preview-title">{previewEngine.name}</h2>
            <p>{previewEngine.description}</p>
            <p>A reliable river-shift workflow needs validated water extents, seasonal controls, channel geometry and uncertainty reporting. It is not yet connected to a production processing pipeline.</p>
            <button className="section-action" type="button" onClick={() => setPreviewEngine(null)}>Close</button>
          </div>
        </div>
      )}
      {showAuthModal && !user && (
        <div className="auth-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="auth-modal-title">
          <div className="auth-modal glass-panel">
            <button className="auth-modal-close" type="button" onClick={onCloseAuthModal} aria-label="Close authentication prompt">×</button>
            <div className="auth-modal-header">
              <span className="brand-mark" aria-hidden="true"><span className="brand-mountain" /><span className="brand-water" /></span>
              <span id="auth-modal-title">Continue to SARGuardian</span>
            </div>
            <p>Sign in with Google to open the analysis workspace.</p>
            <button className="sign-in-button large" onClick={onSignIn} type="button" disabled={authState === 'loading'}>
              <span className="google-mark" aria-hidden="true">G</span>
              {authState === 'loading' ? 'Signing in...' : 'Sign in with Google'}
            </button>
          </div>
        </div>
      )}
      <Footer />
    </div>
  );
}
