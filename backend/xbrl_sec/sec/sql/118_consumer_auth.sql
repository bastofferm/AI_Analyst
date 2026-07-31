-- 118_consumer_auth.sql
-- Consumer app auth + subscription (DOC WA0007 §11 Tech Stack: NextAuth +
-- Stripe alternative). Custom JWT-based auth lives in FastAPI; this migration
-- adds the two backing tables. Idempotent.

CREATE TABLE IF NOT EXISTS sec.mzqa_user (
    id                   BIGSERIAL PRIMARY KEY,
    email                TEXT NOT NULL UNIQUE,
    password_hash        TEXT NOT NULL,           -- bcrypt
    lang                 VARCHAR(2) DEFAULT 'en', -- 'en' | 'de' onboarding pick
    view_mode            VARCHAR(20) DEFAULT 'simple', -- 'simple' | 'advanced' (WA0007 §8)
    trial_started_at     TIMESTAMPTZ DEFAULT NOW(),
    trial_end            TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '7 days'),
    stripe_customer_id   TEXT UNIQUE,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW(),
    last_login_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mzqa_user_email ON sec.mzqa_user (lower(email));

-- One row per Stripe Subscription. Updated by the webhook handler so the
-- requirePro() gate has the latest status without polling Stripe.
CREATE TABLE IF NOT EXISTS sec.mzqa_subscription (
    id                       BIGSERIAL PRIMARY KEY,
    user_id                  BIGINT NOT NULL REFERENCES sec.mzqa_user(id) ON DELETE CASCADE,
    stripe_subscription_id   TEXT UNIQUE NOT NULL,
    status                   TEXT NOT NULL,        -- trialing | active | past_due | canceled | unpaid | incomplete
    price_id                 TEXT,                  -- which Stripe Price (€29/mo)
    current_period_end       TIMESTAMPTZ,
    cancel_at                TIMESTAMPTZ,
    canceled_at              TIMESTAMPTZ,
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    updated_at               TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mzqa_subscription_user ON sec.mzqa_subscription (user_id);
CREATE INDEX IF NOT EXISTS idx_mzqa_subscription_status ON sec.mzqa_subscription (status);
