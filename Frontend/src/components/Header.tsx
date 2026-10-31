import type { User } from 'firebase/auth';
import { ProfileControl } from './ProfileControl';

type HeaderProps = {
  user: User | null;
  authState: 'idle' | 'loading' | 'ready' | 'error';
  onSignIn: () => void;
  onSignOut: () => void;
};

const navItems = [
  { label: 'Home', href: '#home' },
  { label: 'Cases', href: '#cases' },
  { label: 'Method', href: '#method' },
  { label: 'Data', href: '#data' },
  { label: 'Archive', href: '#archive' },
];

export function Header({ user, authState, onSignIn, onSignOut }: HeaderProps) {
  return (
    <header className="site-header">
      <a className="brand-lockup" href="#home" aria-label="SARGuardian home">
        <span className="brand-mark" aria-hidden="true"><span className="brand-mountain" /><span className="brand-water" /></span>
        <span className="brand-copy">
          <strong><b>SAR</b>Guardian</strong>
          <small>EARTH IN SIGHT</small>
        </span>
      </a>

      <nav className="main-nav" aria-label="Main navigation">
        {navItems.map((item, index) => (
          <a className={index === 0 ? 'nav-link is-current' : 'nav-link'} href={item.href} key={item.href}>
            {item.label}
          </a>
        ))}
      </nav>

      <div className="header-actions">
        {user ? (
          <ProfileControl user={user} onSignOut={onSignOut} />
        ) : (
          <button className="sign-in-button" onClick={onSignIn} type="button" disabled={authState === 'loading'}>
            <span className="google-mark" aria-hidden="true">G</span>
            {authState === 'loading' ? 'Signing in...' : 'Sign in'}
          </button>
        )}
      </div>
    </header>
  );
}
