-- Earlier versions keyed rows by a random UUID, which gave no idempotency.
-- Keep any such table around under a different name instead of failing.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'stats' AND column_name = 'id'
    ) THEN
        ALTER TABLE stats RENAME TO stats_legacy;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS stats (
    station    TEXT             NOT NULL,
    sensor     INTEGER          NOT NULL CHECK (sensor >= 0),
    timestamp  BIGINT           NOT NULL,           -- window start, epoch ms
    window_ms  INTEGER          NOT NULL CHECK (window_ms > 0),
    mean       DOUBLE PRECISION NOT NULL,
    min        DOUBLE PRECISION NOT NULL,
    max        DOUBLE PRECISION NOT NULL,
    count      BIGINT           NOT NULL CHECK (count > 0),
    regime     SMALLINT,                            -- ground truth, if known
    -- The window identity doubles as the idempotency key: redelivered
    -- messages hit ON CONFLICT DO NOTHING instead of creating duplicates.
    PRIMARY KEY (station, sensor, timestamp),
    CHECK (min <= max)
);

-- (station, sensor, timestamp DESC) lookups use the primary key; this index
-- serves time-range queries across all stations.
CREATE INDEX IF NOT EXISTS idx_stats_timestamp ON stats (timestamp DESC);
