-- Migration 114: XP Shop — Convert XP to Credits
-- 500 XP = 1 credit, max 5 credits/month

-- Add spent_xp column to track XP used in shop
ALTER TABLE users ADD COLUMN IF NOT EXISTS spent_xp INTEGER DEFAULT 0;

-- XP transaction audit log
CREATE TABLE IF NOT EXISTS xp_transactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('earn_vote', 'earn_correct', 'earn_challenge', 'earn_referral', 'earn_unlock', 'spend_redeem', 'earn_bonus')),
    credits_received INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_xp_transactions_user ON xp_transactions(user_id, created_at);

-- ============================================
-- RPC: Redeem XP for credits atomically
-- ============================================
CREATE OR REPLACE FUNCTION redeem_xp_for_credits(p_user_id UUID, p_credits INTEGER DEFAULT 1)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_xp_cost INTEGER;
    v_available_xp INTEGER;
    v_spent_this_month INTEGER;
    v_max_monthly INTEGER := 5;
    v_xp_per_credit INTEGER := 500;
    v_month_start DATE;
BEGIN
    v_xp_cost := p_credits * v_xp_per_credit;
    v_month_start := DATE_TRUNC('month', CURRENT_DATE)::date;

    -- Validate credit amount
    IF p_credits < 1 OR p_credits > v_max_monthly THEN
        RETURN jsonb_build_object('success', false, 'error', 'INVALID_AMOUNT');
    END IF;

    -- Check available XP (total_xp - spent_xp)
    SELECT COALESCE(total_xp, 0) - COALESCE(spent_xp, 0) INTO v_available_xp
    FROM users WHERE id = p_user_id;

    IF v_available_xp IS NULL THEN
        RETURN jsonb_build_object('success', false, 'error', 'USER_NOT_FOUND');
    END IF;

    IF v_available_xp < v_xp_cost THEN
        RETURN jsonb_build_object('success', false, 'error', 'INSUFFICIENT_XP', 'available', v_available_xp, 'required', v_xp_cost);
    END IF;

    -- Check monthly limit
    SELECT COALESCE(SUM(credits_received), 0) INTO v_spent_this_month
    FROM xp_transactions
    WHERE user_id = p_user_id
      AND type = 'spend_redeem'
      AND created_at >= v_month_start;

    IF v_spent_this_month + p_credits > v_max_monthly THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'MONTHLY_LIMIT',
            'redeemed_this_month', v_spent_this_month,
            'limit', v_max_monthly
        );
    END IF;

    -- Deduct XP and add credits atomically
    UPDATE users
    SET spent_xp = COALESCE(spent_xp, 0) + v_xp_cost,
        credits = credits + p_credits
    WHERE id = p_user_id;

    -- Record transaction
    INSERT INTO xp_transactions (user_id, amount, type, credits_received)
    VALUES (p_user_id, -v_xp_cost, 'spend_redeem', p_credits);

    RETURN jsonb_build_object(
        'success', true,
        'credits_received', p_credits,
        'xp_spent', v_xp_cost,
        'remaining_monthly', v_max_monthly - v_spent_this_month - p_credits
    );
END;
$$;
