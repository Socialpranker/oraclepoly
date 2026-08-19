-- Demote alerts dedup log — anti-spam для PR-C demote_alerts_monitor.
-- Hourly cron не должен звонить TG каждый час пока admin не отреагирует.
-- _load_recent_alerts() читает rows за last 6h, filter'ит candidates.
--
-- См. app/jobs/demote_alerts_monitor.py + research/shadow-gates-methodology-2026-05.

CREATE TABLE IF NOT EXISTS demote_alert_log (
    id          BIGSERIAL    PRIMARY KEY,
    detector    TEXT         NOT NULL,
    pnl_usd     NUMERIC(12, 2) NOT NULL,
    n_settled   INTEGER      NOT NULL,
    reason      TEXT         NOT NULL,
    alerted_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Index для dedupe-query (WHERE alerted_at > now() - 6h GROUP BY detector).
CREATE INDEX IF NOT EXISTS idx_demote_alert_log_recent
    ON demote_alert_log (detector, alerted_at DESC);
