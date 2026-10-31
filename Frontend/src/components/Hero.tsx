type HeroProps = {
  onStartExploring: () => void;
};

export function Hero({ onStartExploring }: HeroProps) {
  return (
    <section className="hero-stage" id="home" aria-labelledby="hero-title">
      <div className="hero-copy-block">
        <p className="hero-kicker">NISAR-BASED EARTH OBSERVATION</p>
        <h1 id="hero-title"><b>SAR</b>Guardian</h1>
        <p className="hero-subtitle">Observe change. Examine evidence.</p>
        <p className="hero-description">
          Explore where the Earth's surface may be changing using NISAR radar observations.
          Select an area, review the available evidence and understand what the data can—and cannot—tell us.
        </p>
        <div className="hero-actions">
          <button className="hero-primary-action" type="button" onClick={onStartExploring}>Start exploring <span aria-hidden="true">→</span></button>
          <a className="hero-secondary-action" href="#how-it-works">How it works</a>
        </div>
      </div>
      <div className="hero-side-note" aria-hidden="true">
        <span className="hero-side-rule" />
        <span>EARTH OBSERVATION<br />SURFACE CHANGE<br />SCIENTIFIC EVIDENCE</span>
      </div>
    </section>
  );
}
