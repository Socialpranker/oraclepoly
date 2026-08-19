-- Phase 1 Stage 7: withdrawal flow.
-- Semi-auto monthly USDC withdrawal с TG approval.
-- Proposed → approved/rejected → completed/failed. Expires в 48h без action.

CREATE TABLE IF NOT EXISTS withdrawals (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    proposed_at           timestamptz NOT NULL DEFAULT now(),
    amount_usd            numeric(10, 2) NOT NULL CHECK (amount_usd > 0),
    destination_address   text NOT NULL,
    status                text NOT NULL CHECK (status IN (
                              'proposed', 'approved', 'completed',
                              'failed', 'rejected', 'expired'
                          )),
    approved_by           text,
    approved_at           timestamptz,
    rejected_by           text,
    rejected_at           timestamptz,
    expires_at            timestamptz NOT NULL,
    tx_hash               text,
    completed_at          timestamptz,
    failure_reason        text,
    monthly_profit_basis  numeric(10, 2) NOT NULL,
    notes                 text
);

CREATE INDEX IF NOT EXISTS idx_withdrawals_status
    ON withdrawals(status);

CREATE INDEX IF NOT EXISTS idx_withdrawals_expires_proposed
    ON withdrawals(expires_at)
    WHERE status = 'proposed';

-- Rollback (manual, не state-tracked):
-- DROP TABLE withdrawals;
