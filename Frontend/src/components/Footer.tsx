const footerLinks = [
  { label: 'Cases', href: '#cases' },
  { label: 'Method', href: '#method' },
  { label: 'Data sources', href: '#data' },
  { label: 'Archive', href: '#archive' },
];

export function Footer() {
  return (
    <footer className="site-footer site-footer-v2">
      <div className="footer-brand">
        <span className="brand-mark" aria-hidden="true"><span className="brand-mountain" /><span className="brand-water" /></span>
        <span className="brand-copy"><strong><b>SAR</b>Guardian</strong><small>EARTH IN SIGHT</small></span>
      </div>
      <div className="footer-links">
        {footerLinks.map((item) => <a href={item.href} key={item.href}>{item.label}</a>)}
        <a href="https://github.com/moshiour0/SARGuardian_Dynamic" target="_blank" rel="noreferrer">GitHub</a>
      </div>
      <p>Satellite observations. Traceable evidence. Clear limitations.</p>
    </footer>
  );
}
