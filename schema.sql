-- ParkPi Database Schema
-- SQLite3
-- Run manually:  sqlite3 parking.db < schema.sql

-- ─────────────────────────────────────────
-- spots: one row per physical parking spot
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS spots (
    id          TEXT PRIMARY KEY,           -- e.g. "A1", "B3"
    status      TEXT NOT NULL               -- 'free' | 'occupied' | 'reserved'
                CHECK(status IN ('free','occupied','reserved'))
                DEFAULT 'free',
    updated_at  TEXT NOT NULL               -- ISO-8601 timestamp
);

-- ─────────────────────────────────────────
-- reservations: pre-booked slots
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    spot_id     TEXT    NOT NULL REFERENCES spots(id),
    plate       TEXT    NOT NULL,           -- vehicle plate number
    date        TEXT    NOT NULL,           -- YYYY-MM-DD
    time_from   TEXT    NOT NULL,           -- HH:MM
    time_to     TEXT    NOT NULL,           -- HH:MM
    status      TEXT    NOT NULL            -- 'active' | 'expired' | 'cancelled'
                CHECK(status IN ('active','expired','cancelled'))
                DEFAULT 'active',
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_spot   ON reservations(spot_id);
CREATE INDEX IF NOT EXISTS idx_reservations_date   ON reservations(date);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);

-- ─────────────────────────────────────────
-- events: audit log of every state change
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    spot_id     TEXT,
    event_type  TEXT NOT NULL,              -- 'state_change' | 'barrier_open' | 'reservation'
    detail      TEXT,
    ts          TEXT NOT NULL
);

-- ─────────────────────────────────────────
-- Seed spots A1–D8 (32 spots)
-- ─────────────────────────────────────────
INSERT OR IGNORE INTO spots (id, status, updated_at) VALUES
  ('A1','free', datetime('now')), ('A2','free', datetime('now')),
  ('A3','free', datetime('now')), ('A4','free', datetime('now')),
  ('A5','free', datetime('now')), ('A6','free', datetime('now')),
  ('A7','free', datetime('now')), ('A8','free', datetime('now')),
  ('B1','free', datetime('now')), ('B2','free', datetime('now')),
  ('B3','free', datetime('now')), ('B4','free', datetime('now')),
  ('B5','free', datetime('now')), ('B6','free', datetime('now')),
  ('B7','free', datetime('now')), ('B8','free', datetime('now')),
  ('C1','free', datetime('now')), ('C2','free', datetime('now')),
  ('C3','free', datetime('now')), ('C4','free', datetime('now')),
  ('C5','free', datetime('now')), ('C6','free', datetime('now')),
  ('C7','free', datetime('now')), ('C8','free', datetime('now')),
  ('D1','free', datetime('now')), ('D2','free', datetime('now')),
  ('D3','free', datetime('now')), ('D4','free', datetime('now')),
  ('D5','free', datetime('now')), ('D6','free', datetime('now')),
  ('D7','free', datetime('now')), ('D8','free', datetime('now'));
