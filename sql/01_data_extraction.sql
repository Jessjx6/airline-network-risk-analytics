-- Stage 1: load airports.dat + routes.dat, clean them, publish vw_clean_network.
--
-- Run from the project root (the .import paths below are relative):
--   sqlite3 supply_chain.db ".read sql/01_data_extraction.sql"

PRAGMA foreign_keys = ON;

-- Drop everything first so the script can be re-run against an existing db.
DROP VIEW  IF EXISTS vw_clean_network;
DROP TABLE IF EXISTS lanes;
DROP TABLE IF EXISTS facilities;
DROP TABLE IF EXISTS raw_routes;
DROP TABLE IF EXISTS raw_airports;

-- Landing tables. Everything is TEXT on purpose: the source files use '\N'
-- for nulls and have the odd malformed number, and we don't want .import
-- choking on a bad row before we've had a chance to filter it.
CREATE TABLE raw_airports (
    airport_id   TEXT,
    name         TEXT,
    city         TEXT,
    country      TEXT,
    iata         TEXT,   -- 3-letter code, our business key
    icao         TEXT,
    latitude     TEXT,
    longitude    TEXT,
    altitude     TEXT,
    timezone     TEXT,
    dst          TEXT,
    tz_db        TEXT,
    type         TEXT,
    source       TEXT
);

CREATE TABLE raw_routes (
    carrier        TEXT,
    carrier_id     TEXT,
    origin_iata    TEXT,
    origin_id      TEXT,
    dest_iata      TEXT,
    dest_id        TEXT,
    codeshare      TEXT,
    stops          TEXT,
    equipment      TEXT
);

-- Note: these are sqlite3 CLI dot-commands, not SQL. They only work when the
-- script is run through the sqlite3 shell, and only from the project root.
.mode csv
.import airports.dat raw_airports
.import routes.dat   raw_routes

-- facilities = the clean node table.
-- Keep a row only if it has a real 3-letter IATA code and usable coordinates.
-- INSERT OR IGNORE + PK on iata means the first valid row for a code wins and
-- later dupes are silently dropped.
CREATE TABLE facilities (
    iata        TEXT PRIMARY KEY,
    name        TEXT,
    city        TEXT,
    country     TEXT,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL
);

INSERT OR IGNORE INTO facilities (iata, name, city, country, latitude, longitude)
SELECT
    TRIM(iata),
    name,
    city,
    country,
    CAST(latitude  AS REAL),
    CAST(longitude AS REAL)
FROM raw_airports
WHERE iata IS NOT NULL
  AND TRIM(iata) NOT IN ('', '\N')
  AND LENGTH(TRIM(iata)) = 3
  AND latitude  IS NOT NULL AND TRIM(latitude)  NOT IN ('', '\N')
  AND longitude IS NOT NULL AND TRIM(longitude) NOT IN ('', '\N')
  -- CAST('garbage' AS REAL) is 0.0 in SQLite, so a 0.0 result is either junk
  -- or a genuine 0 coordinate. Accept it only when the text really was "0".
  AND (CAST(latitude  AS REAL) <> 0.0 OR TRIM(latitude)  IN ('0', '0.0'))
  AND (CAST(longitude AS REAL) <> 0.0 OR TRIM(longitude) IN ('0', '0.0'))
;

-- Catch anything that slipped through with out-of-range coordinates.
DELETE FROM facilities
WHERE latitude  NOT BETWEEN -90  AND 90
   OR longitude NOT BETWEEN -180 AND 180;

-- lanes = the clean edge table, one row per (origin, dest) pair.
-- shipment_frequency is the number of carrier routes on that pair, which
-- becomes the edge weight in the graph. Endpoints must both be known
-- facilities; self-loops are dropped.
CREATE TABLE lanes (
    origin_iata          TEXT NOT NULL,
    dest_iata            TEXT NOT NULL,
    shipment_frequency   INTEGER NOT NULL,
    PRIMARY KEY (origin_iata, dest_iata),
    FOREIGN KEY (origin_iata) REFERENCES facilities(iata),
    FOREIGN KEY (dest_iata)   REFERENCES facilities(iata)
);

INSERT OR IGNORE INTO lanes (origin_iata, dest_iata, shipment_frequency)
SELECT
    TRIM(r.origin_iata),
    TRIM(r.dest_iata),
    COUNT(*)
FROM raw_routes r
WHERE r.origin_iata IS NOT NULL AND TRIM(r.origin_iata) NOT IN ('', '\N')
  AND r.dest_iata   IS NOT NULL AND TRIM(r.dest_iata)   NOT IN ('', '\N')
  AND TRIM(r.origin_iata) <> TRIM(r.dest_iata)
  AND TRIM(r.origin_iata) IN (SELECT iata FROM facilities)
  AND TRIM(r.dest_iata)   IN (SELECT iata FROM facilities)
GROUP BY TRIM(r.origin_iata), TRIM(r.dest_iata);

CREATE INDEX idx_lanes_origin ON lanes(origin_iata);
CREATE INDEX idx_lanes_dest   ON lanes(dest_iata);

-- What Python actually reads: one row per lane, with both endpoints' details
-- joined in. Facilities with no lanes never show up here.
CREATE VIEW vw_clean_network AS
SELECT
    l.origin_iata,
    fo.name      AS origin_name,
    fo.city      AS origin_city,
    fo.country   AS origin_country,
    fo.latitude  AS origin_latitude,
    fo.longitude AS origin_longitude,
    l.dest_iata,
    fd.name      AS dest_name,
    fd.city      AS dest_city,
    fd.country   AS dest_country,
    fd.latitude  AS dest_latitude,
    fd.longitude AS dest_longitude,
    l.shipment_frequency
FROM lanes l
JOIN facilities fo ON fo.iata = l.origin_iata
JOIN facilities fd ON fd.iata = l.dest_iata;

-- Quick row-count sanity check, printed at the end of a CLI run.
SELECT 'raw_airports rows loaded'  AS metric, COUNT(*) AS value FROM raw_airports
UNION ALL
SELECT 'raw_routes rows loaded',             COUNT(*) FROM raw_routes
UNION ALL
SELECT 'clean facilities (nodes)',           COUNT(*) FROM facilities
UNION ALL
SELECT 'clean lanes (edges)',                COUNT(*) FROM lanes
UNION ALL
SELECT 'rows published to vw_clean_network', COUNT(*) FROM vw_clean_network;
