-- Relational schema (Postgres). Vectors live in Qdrant, not here.
-- Calendar is intentionally NOT a table: availability is computed by a
-- deterministic function (see availability.py) to avoid ~18M rows.

CREATE TABLE IF NOT EXISTS listings (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    type                    TEXT NOT NULL,          -- entire place | private room | hotel | ...
    city                    TEXT NOT NULL,
    neighbourhood           TEXT,
    lat                     DOUBLE PRECISION NOT NULL,
    lng                     DOUBLE PRECISION NOT NULL,
    base_price              NUMERIC(10,2) NOT NULL,  -- nightly base; calendar fn perturbs it
    beds                    INT NOT NULL,
    amenities               JSONB NOT NULL DEFAULT '[]',
    photos                  JSONB NOT NULL DEFAULT '[]',
    host                    JSONB NOT NULL DEFAULT '{}',
    rating                  REAL,
    review_count            INT NOT NULL DEFAULT 0,
    neighbourhood_price_pct REAL                     -- enrichment: price percentile in area
);

CREATE INDEX IF NOT EXISTS idx_listings_city        ON listings (city);
CREATE INDEX IF NOT EXISTS idx_listings_type        ON listings (type);
CREATE INDEX IF NOT EXISTS idx_listings_price       ON listings (base_price);
CREATE INDEX IF NOT EXISTS idx_listings_rating      ON listings (rating);
CREATE INDEX IF NOT EXISTS idx_listings_amenities   ON listings USING GIN (amenities);

CREATE TABLE IF NOT EXISTS reviews (
    id           TEXT PRIMARY KEY,
    listing_id   TEXT NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    date         DATE,
    reviewer     TEXT,
    rating       REAL,
    text         TEXT NOT NULL,
    language     TEXT,
    aspects      JSONB,           -- enrichment: {cleanliness, location, value, staff, noise}
    sentiment    REAL             -- overall sentiment score
);

CREATE INDEX IF NOT EXISTS idx_reviews_listing   ON reviews (listing_id);
CREATE INDEX IF NOT EXISTS idx_reviews_language  ON reviews (language);
CREATE INDEX IF NOT EXISTS idx_reviews_rating    ON reviews (rating);
-- Full-text search on review text (Option A: reviews stay in Postgres, not Qdrant).
-- 'simple' config = no language-specific stemming, since reviews are multilingual.
CREATE INDEX IF NOT EXISTS idx_reviews_fts ON reviews USING GIN (to_tsvector('simple', text));
-- Watch index size on the free 0.5GB tier (~100-200MB at 200K reviews).

-- Enrichment: precomputed per-property review summary (brief §2.1 / §2.2).
CREATE TABLE IF NOT EXISTS listing_summaries (
    listing_id   TEXT PRIMARY KEY REFERENCES listings (id) ON DELETE CASCADE,
    summary      TEXT NOT NULL,
    aspect_avg   JSONB,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
