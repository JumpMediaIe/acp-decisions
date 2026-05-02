-- ACP decisions archive — full schema. Used by db.open_db() on every connection.
-- All CREATE statements use IF NOT EXISTS so applying the schema is idempotent.

CREATE TABLE IF NOT EXISTS decisions (
  reference                TEXT PRIMARY KEY,
  decision_date            TEXT NOT NULL,
  county                   TEXT,
  county_raw               TEXT NOT NULL,
  site_address             TEXT,
  development_type_id      TEXT,
  development_type_raw     TEXT NOT NULL,
  decision_outcome         TEXT NOT NULL,
  council_decision         TEXT,
  applicant_name_raw       TEXT,
  inspector_report_url     TEXT,
  scraped_at               TEXT NOT NULL,
  classified_at            TEXT
);

CREATE TABLE IF NOT EXISTS refusal_reasons (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_reference       TEXT NOT NULL,
  reason_number            INTEGER NOT NULL,
  raw_text                 TEXT NOT NULL,
  FOREIGN KEY (decision_reference) REFERENCES decisions(reference) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reason_categories (
  reason_id                INTEGER NOT NULL,
  category_id              TEXT NOT NULL,
  PRIMARY KEY (reason_id, category_id),
  FOREIGN KEY (reason_id) REFERENCES refusal_reasons(id) ON DELETE CASCADE,
  FOREIGN KEY (category_id) REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS categories (
  id                       TEXT PRIMARY KEY,
  name                     TEXT NOT NULL,
  description              TEXT NOT NULL,
  group_label              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrape_errors (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  reference                TEXT,
  error_class              TEXT NOT NULL,
  message                  TEXT,
  occurred_at              TEXT NOT NULL,
  resolved_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_date     ON decisions(decision_date);
CREATE INDEX IF NOT EXISTS idx_decisions_county   ON decisions(county);
CREATE INDEX IF NOT EXISTS idx_decisions_type     ON decisions(development_type_id);
CREATE INDEX IF NOT EXISTS idx_decisions_outcome  ON decisions(decision_outcome);
CREATE INDEX IF NOT EXISTS idx_reasons_decision   ON refusal_reasons(decision_reference);

CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
  reference UNINDEXED,
  site_address,
  reasons_concat,
  content='',
  tokenize='porter unicode61'
);
