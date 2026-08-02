import { Suspense, lazy } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import type { ReactElement } from 'react';

import { useAuth } from './auth/AuthContext';
import { Layout } from './components/Layout';
import { Billing } from './pages/Billing';
import { CodeReview } from './pages/CodeReview';
import { DocQA } from './pages/DocQA';
import { Login } from './pages/Login';
import { Signup } from './pages/Signup';
import { SupportTriage } from './pages/SupportTriage';

// Observability pulls in the whole charting library for a page only admins ever
// open. Splitting it keeps that weight out of the initial bundle entirely.
const Observability = lazy(() =>
  import('./pages/Observability').then((m) => ({ default: m.Observability })),
);

function RequireAuth({
  children,
  adminOnly = false,
}: {
  children: ReactElement;
  adminOnly?: boolean;
}) {
  const { user, loading, isAdmin } = useAuth();

  // Wait for the session probe before deciding. Redirecting first would bounce
  // an already-authenticated user to the login page on every page refresh.
  if (loading) return <div className="page muted">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  if (adminOnly && !isAdmin) return <Navigate to="/doc-qa" replace />;
  return children;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/signup" element={<Signup />} />

      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/doc-qa" element={<DocQA />} />
        <Route path="/code-review" element={<CodeReview />} />
        <Route path="/support" element={<SupportTriage />} />
        <Route path="/billing" element={<Billing />} />
        <Route
          path="/observability"
          element={
            <RequireAuth adminOnly>
              <Suspense fallback={<div className="page muted">Loading metrics…</div>}>
                <Observability />
              </Suspense>
            </RequireAuth>
          }
        />
      </Route>

      <Route path="/" element={<Navigate to="/doc-qa" replace />} />
      <Route path="*" element={<Navigate to="/doc-qa" replace />} />
    </Routes>
  );
}
