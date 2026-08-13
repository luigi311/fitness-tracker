-- SQLite schema shipped by v1.0.0 and v1.1.0.
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

INSERT INTO activities (id, start_time, end_time)
VALUES (1, '2024-01-01 08:00:00', '2024-01-01 08:30:00');
INSERT INTO heart_rate (id, activity_id, timestamp_ms, bpm, rr_interval, energy_kj)
VALUES (1, 1, 1000, 142, 422.5, 1.25);

PRAGMA foreign_keys = ON;
