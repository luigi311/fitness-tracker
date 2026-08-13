-- SQLite schema shipped by v2.0.0.
PRAGMA foreign_keys = OFF;

CREATE TABLE activities (
    id INTEGER NOT NULL,
    start_time DATETIME NOT NULL,
    end_time DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT uq_activities_start_time UNIQUE (start_time)
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
    PRIMARY KEY (id),
    FOREIGN KEY (activity_id) REFERENCES activities (id)
);

CREATE INDEX ix_run_activity_id ON running_metrics (activity_id);
CREATE INDEX ix_run_activity_time ON running_metrics (activity_id, timestamp_ms);

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

INSERT INTO activities (id, start_time, end_time)
VALUES (1, '2024-02-01 08:00:00', '2024-02-01 08:30:00');
INSERT INTO heart_rate (id, activity_id, timestamp_ms, bpm, rr_interval, energy_kj)
VALUES (1, 1, 1000, 142, 422.5, 1.25);
INSERT INTO running_metrics (
    id, activity_id, timestamp_ms, speed_mps, cadence_spm,
    stride_length_m, total_distance_m, power_watts
)
VALUES (1, 1, 1000, 2.8, 172, 1.1, 2.8, 240.0);
INSERT INTO activity_uploads (
    id, activity_id, provider, status, uploaded_at,
    provider_activity_id, payload_hash, last_error
)
VALUES (
    1, 1, 'intervals_icu', 'ok', '2024-02-01 09:00:00',
    'provider-v2-1', 'hash-v2-1', NULL
);

PRAGMA foreign_keys = ON;
