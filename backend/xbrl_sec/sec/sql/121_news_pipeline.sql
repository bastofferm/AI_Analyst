-- RSS news ingestion and article-level sentiment storage.
-- Kept in a dedicated schema so raw news can evolve independently from sec.

CREATE SCHEMA IF NOT EXISTS news;

CREATE TABLE IF NOT EXISTS news.feed_sources (
    feed_key   TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    url        TEXT NOT NULL,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    custom     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS news.articles (
    article_id    BIGSERIAL PRIMARY KEY,
    feed_key      TEXT NOT NULL REFERENCES news.feed_sources(feed_key),
    url           TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    full_content  TEXT,
    author        TEXT,
    published_at  TIMESTAMPTZ,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed     BOOLEAN NOT NULL DEFAULT FALSE,
    fast_lane     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_news_articles_published
    ON news.articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_processed
    ON news.articles (processed);
CREATE INDEX IF NOT EXISTS idx_news_articles_fast_lane
    ON news.articles (fast_lane) WHERE fast_lane;

CREATE TABLE IF NOT EXISTS news.watchlist (
    ticker      TEXT PRIMARY KEY,
    market      TEXT NOT NULL CHECK (market IN ('US', 'JP')),
    proxy_terms TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS news.urgency_triggers (
    phrase  TEXT PRIMARY KEY,
    weight  NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS news.sentiment_scores (
    article_id BIGINT NOT NULL REFERENCES news.articles(article_id) ON DELETE CASCADE,
    ticker     TEXT NOT NULL,
    model      TEXT NOT NULL CHECK (model IN ('finbert', 'qwen', 'deepseek')),
    label      TEXT NOT NULL CHECK (label IN ('positive', 'neutral', 'negative')),
    score      NUMERIC(6,4) NOT NULL CHECK (score >= 0 AND score <= 1),
    rationale  TEXT,
    scored_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (article_id, ticker, model)
);

CREATE INDEX IF NOT EXISTS idx_news_scores_scored
    ON news.sentiment_scores (scored_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_scores_ticker_scored
    ON news.sentiment_scores (ticker, scored_at DESC);

CREATE TABLE IF NOT EXISTS news.ingestion_runs (
    run_id           BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    feeds_polled     INTEGER NOT NULL DEFAULT 0,
    articles_new     INTEGER NOT NULL DEFAULT 0,
    articles_scored  INTEGER NOT NULL DEFAULT 0,
    errors           JSONB
);

INSERT INTO news.feed_sources (feed_key, label, url, enabled, custom)
VALUES
    ('calculated_risk_rss', 'Calculated Risk RSS', 'https://feeds.feedburner.com/CalculatedRisk', TRUE, FALSE),
    ('nyt_business', 'New York Times Business', 'https://rss.nytimes.com/services/xml/rss/nyt/Business.xml', TRUE, FALSE),
    ('bbc_world', 'BBC World', 'https://feeds.bbci.co.uk/news/world/rss.xml', TRUE, FALSE),
    ('marketwatch', 'MarketWatch Top Stories', 'https://feeds.marketwatch.com/marketwatch/topstories/', TRUE, FALSE),
    ('npr_world', 'NPR World', 'https://feeds.npr.org/1004/rss.xml', TRUE, FALSE),
    ('trump_truth', 'Trump Truth', 'https://trumpstruth.org/feed', FALSE, FALSE)
ON CONFLICT (feed_key) DO UPDATE SET
    label = EXCLUDED.label,
    url = EXCLUDED.url,
    custom = EXCLUDED.custom;

INSERT INTO news.urgency_triggers (phrase, weight)
VALUES
    ('Fed rate', 1.0),
    ('FOMC', 1.0),
    ('rate cut', 1.0),
    ('sanctions', 1.0),
    ('tariffs', 1.0),
    ('earnings beat', 1.0),
    ('earnings miss', 1.0),
    ('guidance cut', 1.0)
ON CONFLICT (phrase) DO NOTHING;
