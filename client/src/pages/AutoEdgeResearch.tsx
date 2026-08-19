import { Link } from 'react-router-dom';
import { AutoEdgeDashboard } from '../components/bot/research/AutoEdgeDashboard';

export function AutoEdgeResearch() {
  return (
    <div style={{ minHeight: '100vh', background: 'var(--ed-bg, #0c0c0c)', color: 'var(--ed-ink, #fff)' }}>
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--ed-ink-faint)',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
        }}
      >
        <Link to="/bot" style={{ color: 'var(--ed-ink-dim)', textDecoration: 'none', fontSize: '0.85rem' }}>
          ← Bot
        </Link>
        <span style={{ color: 'var(--ed-ink-dim)' }}>/</span>
        <span style={{ fontSize: '0.85rem' }}>Research / Auto Edge</span>
      </div>
      <AutoEdgeDashboard />
    </div>
  );
}
