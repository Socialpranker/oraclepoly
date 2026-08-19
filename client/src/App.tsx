import { lazy, Suspense } from 'react';
import * as Sentry from '@sentry/react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './components/AuthProvider';
import { SemanticSkeleton } from './components/ui/SemanticSkeleton';
import { useAuth } from './hooks/useAuth';
import './index.css';

/**
 * lazy() с автоматическим retry при ошибке загрузки чанка.
 * После деплоя старые хеши чанков удаляются — перезагрузка страницы подгрузит свежий index.html.
 *
 * Один общий retry flag с TTL 30s: если сразу после reload снова не грузится тот же чанк —
 * показываем понятный fallback вместо infinite reload loop. Через 30s flag протухает,
 * следующая попытка снова получит auto-retry (например, после нового deploy).
 */
function lazyWithRetry<T extends React.ComponentType>(
    factory: () => Promise<{ default: T }>,
) {
    return lazy(() =>
        factory().catch((err) => {
            const key = 'chunk_retry_ts';
            const now = Date.now();
            const last = Number(sessionStorage.getItem(key) ?? 0);
            const RETRY_TTL_MS = 30_000;

            if (!last || now - last > RETRY_TTL_MS) {
                sessionStorage.setItem(key, String(now));
                window.location.reload();
                return new Promise<{ default: T }>(() => {});
            }
            // Уже ретраили недавно — отдадим понятную ошибку наверх
            console.error('Chunk load failed after retry', err);
            throw new Error('Failed to load page — try Cmd+Shift+R or clear cache');
        }),
    );
}

const BotDashboard = lazyWithRetry(() => import('./pages/BotDashboard').then(m => ({ default: m.BotDashboard })));
const StrategyDetail = lazyWithRetry(() => import('./pages/StrategyDetail').then(m => ({ default: m.StrategyDetail })));
const BotHistory = lazyWithRetry(() => import('./pages/BotHistory').then(m => ({ default: m.BotHistory })));
const AutoEdgeResearch = lazyWithRetry(() => import('./pages/AutoEdgeResearch').then(m => ({ default: m.AutoEdgeResearch })));
const Tier2Insights = lazyWithRetry(() => import('./pages/Tier2Insights').then(m => ({ default: m.Tier2Insights })));
const LoginPage = lazyWithRetry(() => import('./pages/LoginPage').then(m => ({ default: m.LoginPage })));

const SentryFallback = ({ error, componentStack }: { error?: Error | unknown; componentStack?: string }) => {
  const err = error as Error | undefined;
  const errorText =
    err?.message ||
    err?.toString?.() ||
    (typeof error === 'string' ? error : JSON.stringify(error, null, 2)) ||
    '(no error message — check console)';
  const stack = err?.stack || '';
  return (
    <div style={{ padding: '1rem', color: '#fff', background: '#0c0c0c', minHeight: '100vh' }}>
      <h1 style={{ fontSize: '1.1rem', marginBottom: '0.5rem', color: '#f87171' }}>Something went wrong</h1>
      <p style={{ color: '#888', fontSize: '0.75rem', marginBottom: '1rem' }}>URL: {location.pathname}{location.search}</p>
      <pre style={{ color: '#f87171', fontSize: '0.7rem', textAlign: 'left', maxWidth: '100%', overflow: 'auto', padding: '0.75rem', background: '#1a1a1a', borderRadius: '0.5rem', marginBottom: '0.75rem', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        <b style={{ color: '#fbbf24' }}>ERROR:</b> {errorText}
        {stack && <><br /><br /><b style={{ color: '#60a5fa' }}>STACK:</b><br />{stack.slice(0, 800)}</>}
        {componentStack && <><br /><br /><b style={{ color: '#a78bfa' }}>COMPONENT STACK:</b>{componentStack.slice(0, 600)}</>}
      </pre>
      <div style={{ display: 'flex', gap: '0.5rem' }}>
        <button
          onClick={() => window.location.reload()}
          style={{ padding: '0.5rem 1.5rem', background: '#00FF88', color: '#000', border: 'none', borderRadius: '0.5rem', fontWeight: 600, cursor: 'pointer' }}
        >
          Reload
        </button>
        <button
          onClick={() => {
            const txt = `URL: ${location.pathname}${location.search}\n\nERROR: ${errorText}\n\nSTACK:\n${stack}\n\nCOMPONENT:\n${componentStack || ''}`;
            navigator.clipboard?.writeText(txt).then(
              () => alert('Copied!'),
              () => alert(txt),
            );
          }}
          style={{ padding: '0.5rem 1.5rem', background: '#333', color: '#fff', border: '1px solid #555', borderRadius: '0.5rem', fontWeight: 600, cursor: 'pointer' }}
        >
          Copy error
        </button>
      </div>
    </div>
  );
};

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="flex items-center justify-center h-screen p-6"><SemanticSkeleton variant="chart" trend="neutral" message="Loading..." /></div>;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function App() {
  return (
    <Sentry.ErrorBoundary fallback={<SentryFallback />}>
      <AuthProvider>
        <BrowserRouter>
          <Suspense fallback={<div className="flex items-center justify-center h-screen p-6"><SemanticSkeleton variant="chart" trend="neutral" message="Loading..." /></div>}>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/" element={<Navigate to="/bot" replace />} />
              <Route path="/bot" element={<RequireAuth><BotDashboard /></RequireAuth>} />
              <Route path="/bot/history" element={<RequireAuth><BotHistory /></RequireAuth>} />
              <Route path="/bot/research/auto-edge" element={<RequireAuth><AutoEdgeResearch /></RequireAuth>} />
              <Route path="/bot/research/tier2" element={<RequireAuth><Tier2Insights /></RequireAuth>} />
              <Route path="/bot/:strategyKey/history" element={<RequireAuth><BotHistory /></RequireAuth>} />
              <Route path="/bot/:strategyKey" element={<RequireAuth><StrategyDetail /></RequireAuth>} />
              <Route path="*" element={<Navigate to="/bot" replace />} />
            </Routes>
          </Suspense>
        </BrowserRouter>
      </AuthProvider>
    </Sentry.ErrorBoundary>
  );
}

export default App;
