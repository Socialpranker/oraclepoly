-- Migration 120: Drop dead schema found during audit
--
-- Removes columns, indexes, and functions that are defined but never
-- read or used by any application code.

-- ============================================
-- 1. users: drop unused columns
-- ============================================
ALTER TABLE users DROP COLUMN IF EXISTS is_admin;
ALTER TABLE users DROP COLUMN IF EXISTS last_login_at;
ALTER TABLE users DROP COLUMN IF EXISTS referred_by;

-- Update process_referral RPC to remove the referred_by write
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
-- 2. whales: drop unused column
-- ============================================
ALTER TABLE whales DROP COLUMN IF EXISTS first_seen_at;

-- ============================================
-- 3. transactions: drop unused columns + index
-- ============================================
DROP INDEX IF EXISTS idx_transactions_user_id;
ALTER TABLE transactions DROP COLUMN IF EXISTS user_id;
ALTER TABLE transactions DROP COLUMN IF EXISTS price;

-- ============================================
-- 4. Drop unused RPC function
-- ============================================
DROP FUNCTION IF EXISTS get_user_votes_today(UUID);
