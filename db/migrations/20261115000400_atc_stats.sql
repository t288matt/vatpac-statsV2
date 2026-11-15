-- migrate:up
-- Issue 387: every ATC session on the network (atc_stats_session) and a log of the loader's runs (atc_stats_run_log).
-- Written by the stats loader (stats_loader/src/vatsim_stats); the role and its grants are in db/roles.sql.
-- All times are UTC (timestamptz). No coverage table: a UTC date that has sessions is loaded, a date with none is missing.

CREATE TABLE atc_stats_session (
    connection_id       bigint      PRIMARY KEY,               -- VATSIM's own id: loading a day twice gives the same rows
    cid                 bigint      NOT NULL,                  -- the controller
    callsign            text        NOT NULL,                  -- as recorded
    -- The last three letters of the callsign, filled in by Postgres. No list and no check, so new combinations just appear.
    position_type       text        GENERATED ALWAYS AS (upper(right(callsign, 3))) STORED,
    rating              smallint,
    session_start       timestamptz NOT NULL,
    session_end         timestamptz,                           -- null only while VATSIM has not closed the session
    -- Null without an end. Same answer for the same start and end whatever the time zone or clock.
    duration_seconds    integer     GENERATED ALWAYS AS (extract(epoch FROM (session_end - session_start))::integer) STORED,
    server              text,
    aircrafttracked     integer     NOT NULL DEFAULT 0,
    aircraftseen        integer     NOT NULL DEFAULT 0,
    flightsamended      integer     NOT NULL DEFAULT 0,
    handoffsinitiated   integer     NOT NULL DEFAULT 0,
    handoffsreceived    integer     NOT NULL DEFAULT 0,
    handoffsrefused     integer     NOT NULL DEFAULT 0,
    squawksassigned     integer     NOT NULL DEFAULT 0,
    cruisealtsmodified  integer     NOT NULL DEFAULT 0,
    tempaltsmodified    integer     NOT NULL DEFAULT 0,
    scratchpadmods      integer     NOT NULL DEFAULT 0,
    loaded_at           timestamptz NOT NULL DEFAULT now(),    -- first stored
    updated_at          timestamptz NOT NULL DEFAULT now()     -- last changed by a later load
);

-- Every session of one controller; one callsign over a period.
CREATE INDEX atc_stats_session_cid_idx ON atc_stats_session (cid, session_start DESC);
CREATE INDEX atc_stats_session_callsign_idx ON atc_stats_session (callsign, session_start DESC);
-- A very small index of the date range in each block of the table: date windows and the retention delete avoid a full scan.
CREATE INDEX atc_stats_session_start_brin ON atc_stats_session USING brin (session_start);

CREATE TABLE atc_stats_run_log (
    id                bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    last_day_at       timestamptz,                             -- when the run last wrote a day: tells a working run from a stuck one
    requests          integer     NOT NULL DEFAULT 0,
    sessions_inserted integer     NOT NULL DEFAULT 0,
    sessions_updated  integer     NOT NULL DEFAULT 0,
    sessions_deleted  integer     NOT NULL DEFAULT 0,
    days_loaded       integer     NOT NULL DEFAULT 0,
    failed_days       date[]      NOT NULL DEFAULT '{}',       -- dates that were not written; the reason is in error
    hour_windows_from date,                                    -- from this date back, days need one-hour windows
    empty_dates       date[]      NOT NULL DEFAULT '{}',       -- dates confirmed empty on all 24 hours
    status            text        NOT NULL DEFAULT 'running'
                                  CHECK (status IN ('running', 'success', 'failed', 'stopped', 'interrupted')),
    error             text
);

-- migrate:down
-- Forward-only project: no automatic rollback.
