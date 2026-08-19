-- Migration 115: Referral System
-- Track referrals, award XP to both sides

CREATE TABLE IF NOT EXISTS referrals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    referrer_id UUID NOT NULL REFERENCES users(id),
    referred_id UUID NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'registered' CHECK (status IN ('registered', 'activated')),
    xp_awarded INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(referred_id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);

-- ============================================
-- RPC: Process a referral
-- Called when a new user registers with ref code
-- ============================================
CREATE OR REPLACE FUNCTION process_referral(p_referrer_telegram_id BIGINT, p_referred_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_referrer_id UUID;
    v_monthly_count INTEGER;
    v_registration_xp INTEGER := 100;
BEGIN
    -- Find referrer
    SELECT id INTO v_referrer_id FROM users WHERE telegram_id = p_referrer_telegram_id;
    IF v_referrer_id IS NULL THEN
        RETURN jsonb_build_object('success', false, 'error', 'REFERRER_NOT_FOUND');
    END IF;

    -- Don't allow self-referral
    IF v_referrer_id = p_referred_user_id THEN
        RETURN jsonb_build_object('success', false, 'error', 'SELF_REFERRAL');
    END IF;

    -- Check monthly limit (20 referrals/month)
    SELECT COUNT(*) INTO v_monthly_count
    FROM referrals
    WHERE referrer_id = v_referrer_id
      AND created_at >= DATE_TRUNC('month', CURRENT_DATE);

    IF v_monthly_count >= 20 THEN
        RETURN jsonb_build_object('success', false, 'error', 'MONTHLY_LIMIT');
    END IF;

    -- Check if already referred
    IF EXISTS (SELECT 1 FROM referrals WHERE referred_id = p_referred_user_id) THEN
        RETURN jsonb_build_object('success', false, 'error', 'ALREADY_REFERRED');
    END IF;

    -- Record referral
    INSERT INTO referrals (referrer_id, referred_id, status, xp_awarded)
    VALUES (v_referrer_id, p_referred_user_id, 'registered', v_registration_xp);

    -- Award XP to referrer (+100 for registration)
    UPDATE users
    SET xp = xp + v_registration_xp,
        total_xp = total_xp + v_registration_xp
    WHERE id = v_referrer_id;

    -- Award bonus credits to referred user (+2)
    UPDATE users
    SET credits = credits + 2
    WHERE id = p_referred_user_id;

    RETURN jsonb_build_object('success', true, 'referrer_xp', v_registration_xp, 'referred_credits', 2);
END;
$$;

-- ============================================
-- RPC: Activate referral (when referred user casts first vote)
-- Awards +50 XP to referrer
-- ============================================
CREATE OR REPLACE FUNCTION activate_referral(p_referred_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_referral RECORD;
    v_activation_xp INTEGER := 50;
BEGIN
    SELECT * INTO v_referral
    FROM referrals
    WHERE referred_id = p_referred_user_id AND status = 'registered';

    IF v_referral IS NULL THEN
        RETURN jsonb_build_object('success', false, 'error', 'NO_PENDING_REFERRAL');
    END IF;

    -- Update status
    UPDATE referrals
    SET status = 'activated',
        xp_awarded = xp_awarded + v_activation_xp
    WHERE id = v_referral.id;

    -- Award activation bonus to referrer
    UPDATE users
    SET xp = xp + v_activation_xp,
        total_xp = total_xp + v_activation_xp
    WHERE id = v_referral.referrer_id;

    RETURN jsonb_build_object('success', true, 'activation_xp', v_activation_xp);
END;
$$;
