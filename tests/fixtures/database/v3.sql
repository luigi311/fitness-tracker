-- SQLite schema shipped by v3.0.0.
PRAGMA foreign_keys = OFF;

CREATE TABLE activities (
    id INTEGER NOT NULL,
    start_time DATETIME NOT NULL,
    end_time DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT uq_activities_start_time UNIQUE (start_time)
);

CREATE TABLE activity_sport (
    id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    sport_type_id INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_activity_sport_activity_id UNIQUE (activity_id),
    FOREIGN KEY (activity_id) REFERENCES activities (id) ON DELETE CASCADE
);

CREATE TABLE heart_rate (
    id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    timestamp_ms BIGINT NOT NULL,
    bpm INTEGER NOT NULL,
    rr_interval FLOAT,
    energy_kj FLOAT,
    PRIMARY KEY (id),
    FOREIGN KEY (activity_id) REFERENCES activities (id)
);

CREATE INDEX ix_hr_activity_id ON heart_rate (activity_id);
CREATE INDEX ix_hr_activity_time ON heart_rate (activity_id, timestamp_ms);

CREATE TABLE running_metrics (
    id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    timestamp_ms BIGINT NOT NULL,
    speed_mps FLOAT NOT NULL,
    cadence_spm INTEGER NOT NULL,
    stride_length_m FLOAT,
    total_distance_m FLOAT,
    power_watts FLOAT,
    incline_percent FLOAT,
    altitude_m FLOAT,
    PRIMARY KEY (id),
    FOREIGN KEY (activity_id) REFERENCES activities (id)
);

CREATE INDEX ix_run_activity_id ON running_metrics (activity_id);
CREATE INDEX ix_run_activity_time ON running_metrics (activity_id, timestamp_ms);

CREATE TABLE cycling_metrics (
    id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    timestamp_ms BIGINT NOT NULL,
    speed_mps FLOAT NOT NULL,
    cadence_rpm INTEGER,
    total_distance_m FLOAT,
    power_watts FLOAT,
    incline_percent FLOAT,
    altitude_m FLOAT,
    PRIMARY KEY (id),
    FOREIGN KEY (activity_id) REFERENCES activities (id)
);

CREATE INDEX ix_cyc_activity_id ON cycling_metrics (activity_id);
CREATE INDEX ix_cyc_activity_time ON cycling_metrics (activity_id, timestamp_ms);

CREATE TABLE activity_uploads (
    id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    provider VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    uploaded_at DATETIME,
    provider_activity_id VARCHAR(128),
    payload_hash VARCHAR(64),
    last_error TEXT,
    PRIMARY KEY (id),
    CONSTRAINT uq_activity_provider UNIQUE (activity_id, provider),
    FOREIGN KEY (activity_id) REFERENCES activities (id) ON DELETE CASCADE
);

CREATE INDEX ix_upload_provider_status ON activity_uploads (provider, status);
CREATE INDEX ix_upload_activity ON activity_uploads (activity_id);

CREATE TABLE activity_stats (
    id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    sport_type_id INTEGER NOT NULL,
    start_time DATETIME NOT NULL,
    end_time DATETIME,
    duration_s INTEGER NOT NULL,
    distance_m FLOAT,
    avg_speed_mps FLOAT,
    avg_bpm FLOAT,
    max_bpm INTEGER,
    total_energy_kj FLOAT NOT NULL,
    avg_cadence FLOAT,
    avg_power_watts FLOAT,
    total_ascent_m FLOAT,
    total_descent_m FLOAT,
    computed_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_activity_stats_activity_id UNIQUE (activity_id),
    FOREIGN KEY (activity_id) REFERENCES activities (id) ON DELETE CASCADE
);

CREATE INDEX ix_stats_activity_id ON activity_stats (activity_id);
CREATE INDEX ix_stats_start_time ON activity_stats (start_time);
CREATE INDEX ix_stats_sport ON activity_stats (sport_type_id);

INSERT INTO activities (id, start_time, end_time)
VALUES (1, '2024-03-01 08:00:00', '2024-03-01 08:30:00');
INSERT INTO activity_sport (id, activity_id, sport_type_id)
VALUES (1, 1, 1);
INSERT INTO heart_rate (id, activity_id, timestamp_ms, bpm, rr_interval, energy_kj)
VALUES (1, 1, 1000, 142, 422.5, 1.25);
INSERT INTO running_metrics (
    id, activity_id, timestamp_ms, speed_mps, cadence_spm,
    stride_length_m, total_distance_m, power_watts, incline_percent, altitude_m
)
VALUES (1, 1, 1000, 2.8, 172, 1.1, 2.8, 240.0, 1.5, 120.0);
INSERT INTO cycling_metrics (
    id, activity_id, timestamp_ms, speed_mps, cadence_rpm,
    total_distance_m, power_watts, incline_percent, altitude_m
)
VALUES (1, 1, 1000, 6.2, 90, 6.2, 210.0, 2.0, 120.0);
INSERT INTO activity_uploads (
    id, activity_id, provider, status, uploaded_at,
    provider_activity_id, payload_hash, last_error
)
VALUES (
    1, 1, 'intervals_icu', 'ok', '2024-03-01 09:00:00',
    'provider-v3-1', 'hash-v3-1', NULL
);
INSERT INTO activity_stats (
    id, activity_id, sport_type_id, start_time, end_time, duration_s,
    distance_m, avg_speed_mps, avg_bpm, max_bpm, total_energy_kj,
    avg_cadence, avg_power_watts, total_ascent_m, total_descent_m, computed_at
)
VALUES (
    1, 1, 1, '2024-03-01 08:00:00', '2024-03-01 08:30:00', 1800,
    5000.0, 2.8, 140.0, 160, 0.0,
    172.0, 240.0, 100.0, 90.0, '2024-03-01 09:00:00'
);

PRAGMA foreign_keys = ON;
