-- migrate:up
-- Core schema, SPEC section 6. All timestamps are timestamptz (UTC).

CREATE TABLE IF NOT EXISTS app_setting (
    key   text PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO app_setting (key, value) VALUES
    ('home_icao_prefixes', 'Y'),
    ('airborne_gap_cap_seconds', '40')
ON CONFLICT (key) DO NOTHING;

-- Lookups (analytics seeds airline, aircraft_type, region_map later).
CREATE TABLE IF NOT EXISTS controller_rating (id int PRIMARY KEY, short text NOT NULL, long text NOT NULL);
CREATE TABLE IF NOT EXISTS facility          (id int PRIMARY KEY, short text NOT NULL, long text NOT NULL);
CREATE TABLE IF NOT EXISTS pilot_rating      (id int PRIMARY KEY, short text NOT NULL, long text NOT NULL);
CREATE TABLE IF NOT EXISTS airline           (prefix text PRIMARY KEY, name text NOT NULL);
CREATE TABLE IF NOT EXISTS aircraft_type     (icao text PRIMARY KEY, category text, is_military boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS region_map        (prefix text PRIMARY KEY, region text NOT NULL);

-- Pilot sessions: identity is (cid, callsign, logon_time); plan fields are attributes only.
CREATE TABLE IF NOT EXISTS pilot_session (
    id              bigserial PRIMARY KEY,
    cid             bigint      NOT NULL,
    callsign        text        NOT NULL CHECK (callsign <> ''),
    logon_time      timestamptz NOT NULL,
    name            text,
    home_icao       text CHECK (home_icao ~ '^[A-Z]{4}$'),
    server          text,
    first_seen      timestamptz NOT NULL,
    last_seen       timestamptz NOT NULL,
    departure       text,
    arrival         text,
    aircraft_icao   text,
    aircraft_faa    text,
    flight_rules    text,
    route           text,
    remarks         text,
    planned_alt     text,
    deptime         text,
    plan_changed_at timestamptz,
    max_gs_kt       int,
    max_alt_ft      int,
    pilot_rating    int,
    military_rating int,
    CONSTRAINT pilot_session_identity UNIQUE (cid, callsign, logon_time),
    CONSTRAINT pilot_session_seen_order CHECK (last_seen >= first_seen)
);
CREATE INDEX IF NOT EXISTS pilot_session_last_seen_idx ON pilot_session (last_seen);

-- Raw positions, native daily partitions pilot_position_YYYYMMDD (see ensure_/drop_ functions).
CREATE TABLE IF NOT EXISTS pilot_position (
    observed_at timestamptz      NOT NULL,
    session_id  bigint           NOT NULL REFERENCES pilot_session (id),
    lat         double precision,
    lon         double precision,
    alt_ft      int CHECK (alt_ft BETWEEN -1500 AND 65000),
    gs_kt       int CHECK (gs_kt BETWEEN 0 AND 700),
    hdg         int CHECK (hdg BETWEEN 0 AND 360),
    PRIMARY KEY (session_id, observed_at)
) PARTITION BY RANGE (observed_at);

CREATE TABLE IF NOT EXISTS controller_session (
    id            bigserial PRIMARY KEY,
    cid           bigint      NOT NULL,
    callsign      text        NOT NULL CHECK (callsign <> ''),
    logon_time    timestamptz NOT NULL,
    name          text,
    rating        int,
    facility      int,
    server        text,
    frequency_hz  bigint,
    visual_range  int,
    first_seen    timestamptz NOT NULL,
    last_seen     timestamptz NOT NULL,
    CONSTRAINT controller_session_identity UNIQUE (cid, callsign, logon_time),
    CONSTRAINT controller_session_seen_order CHECK (last_seen >= first_seen)
);
CREATE INDEX IF NOT EXISTS controller_session_last_seen_idx ON controller_session (last_seen);

CREATE TABLE IF NOT EXISTS controller_rating_event (
    session_id  bigint      NOT NULL REFERENCES controller_session (id),
    observed_at timestamptz NOT NULL,
    rating      int,
    PRIMARY KEY (session_id, observed_at)
);

CREATE TABLE IF NOT EXISTS controller_frequency_event (
    session_id   bigint      NOT NULL REFERENCES controller_session (id),
    observed_at  timestamptz NOT NULL,
    frequency_hz bigint,
    PRIMARY KEY (session_id, observed_at)
);

CREATE TABLE IF NOT EXISTS network_snapshot (
    observed_at           timestamptz PRIMARY KEY,
    pilots_in_region      int NOT NULL,
    controllers_in_region int NOT NULL,
    pilots_global         int NOT NULL,
    controllers_global    int NOT NULL
);

-- One row per poll attempt (ok, skipped_unchanged, error). Retention 400 days (maintenance trims).
CREATE TABLE IF NOT EXISTS poll_log (
    id               bigserial PRIMARY KEY,
    polled_at        timestamptz NOT NULL DEFAULT now(),
    status           text NOT NULL CHECK (status IN ('ok', 'skipped_unchanged', 'error')),
    snapshot_ts      timestamptz,
    duration_ms      int,
    http_status      int,
    pilots_seen      int,
    pilots_inside    int,
    pilots_with_plan int,
    pilots_kept      int,
    controllers_seen int,
    controllers_kept int,
    rejected_values  int,
    polygon_sha      text,
    whitelist_sha    text,
    error            text
);
CREATE INDEX IF NOT EXISTS poll_log_polled_at_idx ON poll_log (polled_at);

CREATE TABLE IF NOT EXISTS controller_unlisted_sighting (
    callsign   text PRIMARY KEY,
    cid        bigint,
    first_seen timestamptz NOT NULL,
    last_seen  timestamptz NOT NULL,
    polls_seen bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS reference_data_change (
    id         bigserial PRIMARY KEY,
    changed_at timestamptz NOT NULL DEFAULT now(),
    file       text NOT NULL,
    old_sha    text,
    new_sha    text NOT NULL
);

-- migrate:down
-- Forward-only project: no automatic rollback of the core schema.
