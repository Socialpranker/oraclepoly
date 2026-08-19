-- Migration 124: Sparks Economy
--
-- Rebrands "Credits" → "Sparks" (Искры) and adjusts all pricing.
-- 10 Sparks = 1 insight unlock. Base rate: $0.30 per insight.
--
-- Reference: docs/unit-economics.md v2.0

-- ============================================
-- 1. Subscription plans: new pricing
-- ============================================
UPDATE subscription_plans
SET price_usd = 4.99,
    credits_per_month = 250,
    features = '["250 Sparks/month (25 insights)", "Whale Activity Alerts", "Basic Oracle Score"]'
WHERE name = 'standard';

UPDATE subscription_plans
SET price_usd = 14.99,
    credits_per_month = 800,
    features = '["800 Sparks/month (80 insights)", "Black Swan Risk Analysis", "Whale Confidence Metrics", "Priority News Sync"]'
WHERE name = 'pro';

INSERT INTO subscription_plans (name, display_name, price_usd, credits_per_month, features)
VALUES ('apex', 'Apex', 29.99, 2000,
        '["2000 Sparks/month (200 insights)", "All Pro features", "Unlimited Votes", "Early Access to New Features"]')
ON CONFLICT (name) DO UPDATE SET
    price_usd = EXCLUDED.price_usd,
    credits_per_month = EXCLUDED.credits_per_month,
    features = EXCLUDED.features;

-- ============================================
-- 2. Credit packs: replace old packs with Sparks packs
-- ============================================
-- Deactivate old packs
UPDATE credit_packs SET is_active = false WHERE name IN ('starter', 'standard', 'bulk');

-- Insert new Sparks packs
INSERT INTO credit_packs (name, credits, price_usd) VALUES
('spark_starter',  50,   1.49),
('spark_explorer', 200,  4.99),
('spark_analyst',  500,  9.99),
('spark_whale',    1500, 19.99)
ON CONFLICT (name) DO UPDATE SET
    credits = EXCLUDED.credits,
    price_usd = EXCLUDED.price_usd,
    is_active = true;

-- ============================================
-- 3. Level perks: scale bonus_credits ×10
-- ============================================
UPDATE level_perks SET bonus_credits = 10 WHERE level = 2;
UPDATE level_perks SET bonus_credits = 30 WHERE level = 7;
UPDATE level_perks SET bonus_credits = 50 WHERE level = 15;

-- ============================================
-- 4. deduct_credit: change default from 1 → 10
-- ============================================
CREATE OR REPLACE FUNCTION deduct_credit(p_telegram_id BIGINT, p_amount INT DEFAULT 10)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_remaining INT;
BEGIN
    UPDATE users
    SET credits = credits - p_amount
    WHERE telegram_id = p_telegram_id
      AND credits >= p_amount
    RETURNING credits INTO v_remaining;

    IF NOT FOUND THEN
        RETURN -1;
    END IF;

    RETURN v_remaining;
END;
$$;

-- ============================================
-- 5. grant_credits: add search_path for security
-- ============================================
CREATE OR REPLACE FUNCTION grant_credits(p_telegram_id BIGINT, p_amount INT)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_new_credits INT;
BEGIN
    UPDATE users
    SET credits = credits + p_amount
    WHERE telegram_id = p_telegram_id
    RETURNING credits INTO v_new_credits;

    IF NOT FOUND THEN
        RETURN -1;
    END IF;

    RETURN v_new_credits;
END;
$$;

-- ============================================
-- 6. redeem_xp_for_credits: max_monthly 5 → 50
-- ============================================
CREATE OR REPLACE FUNCTION redeem_xp_for_credits(p_user_id UUID, p_credits INTEGER DEFAULT 10)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_xp_cost INTEGER;
    v_available_xp INTEGER;
    v_spent_this_month INTEGER;
    v_max_monthly INTEGER := 50;
    v_xp_per_credit INTEGER := 500;
    v_month_start DATE;
BEGIN
    v_xp_cost := p_credits * v_xp_per_credit;
    v_month_start := DATE_TRUNC('month', CURRENT_DATE)::date;

    -- Validate amount
    IF p_credits < 10 OR p_credits > v_max_monthly THEN
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

-- ============================================
-- 7. process_referral: bonus 2 → 20 Sparks
-- ============================================
CREATE OR REPLACE FUNCTION process_referral(p_referrer_telegram_id BIGINT, p_referred_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
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

    -- Award bonus Sparks to referred user (+20)
    UPDATE users
    SET credits = credits + 20
    WHERE id = p_referred_user_id;

    RETURN jsonb_build_object('success', true, 'referrer_xp', v_registration_xp, 'referred_credits', 20);
END;
$$;
