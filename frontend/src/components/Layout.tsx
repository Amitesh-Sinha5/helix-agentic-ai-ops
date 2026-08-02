import { NavLink, Outlet, useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { EscalationPanel } from './EscalationPanel';

const NAV = [
  { to: '/doc-qa', label: 'Doc Q&A' },
  { to: '/code-review', label: 'Code Review' },
  { to: '/support', label: 'Support Triage' },
  { to: '/billing', label: 'Billing' },
];

export function Layout() {
  const { user, isAdmin, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout();
    navigate('/login');
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            ⬡
          </span>
          <span className="brand-name">Helix</span>
        </div>

        <nav className="app-nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-link${isActive ? ' is-active' : ''}`}
            >
              {item.label}
            </NavLink>
          ))}
          {isAdmin && (
            <NavLink
              to="/observability"
              className={({ isActive }) => `nav-link${isActive ? ' is-active' : ''}`}
            >
              Observability
            </NavLink>
          )}
        </nav>

        <div className="app-user">
          <span className="user-email">{user?.email}</span>
          {isAdmin && <span className="badge badge-admin">admin</span>}
          <button type="button" className="button button-ghost" onClick={handleLogout}>
            Log out
          </button>
        </div>
      </header>

      <main className="app-main">
        <Outlet />
      </main>

      {isAdmin && <EscalationPanel />}
    </div>
  );
}
