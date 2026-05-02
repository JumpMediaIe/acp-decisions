-- ACP decisions archive — full schema. Used by db.open_db() on every connection.
-- All CREATE statements use IF NOT EXISTS so applying the schema is idempotent.
--
-- Reference formats (see docs/discovery-2026-05-02.md):
--   case_id_url      numeric URL ID, e.g. 315183. Natural scrape key.
--   abp_reference    canonical "ABP-{id}-{yy}", e.g. "ABP-315183-22". From PDF.
--   pa_reference     page-header form, e.g. "LH02.319750", "PA09.300506".

CREATE TABLE IF NOT EXISTS decisions (
  case_id_url              INTEGER PRIMARY KEY,
  abp_reference            TEXT,
  pa_reference             TEXT,
  decision_date            TEXT NOT NULL,
  county                   TEXT,
  county_raw               TEXT NOT NULL,
  site_address             TEXT,
  development_type_id      TEXT,
  development_type_raw     TEXT NOT NULL,
  case_type_raw            TEXT,
  decision_outcome         TEXT NOT NULL,        -- normalised bucket
  decision_outcome_raw     TEXT NOT NULL,        -- verbatim free text from page
  council_decision         TEXT,
  applicant_name_raw       TEXT,
  scraped_at               TEXT NOT NULL,
  classified_at            TEXT
);

CREATE TABLE IF NOT EXISTS documents (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id_url              INTEGER NOT NULL,
  doc_type                 TEXT NOT NULL,        -- order | inspector_report | direction | bmr | other
  url                      TEXT NOT NULL,
  fetched_at               TEXT,
  FOREIGN KEY (case_id_url) REFERENCES decisions(case_id_url) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS refusal_reasons (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id_url              INTEGER NOT NULL,
  reason_number            INTEGER NOT NULL,
  raw_text                 TEXT NOT NULL,
  FOREIGN KEY (case_id_url) REFERENCES decisions(case_id_url) ON DELETE CASCADE
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
  case_id_url              INTEGER,
  error_class              TEXT NOT NULL,
  message                  TEXT,
  occurred_at              TEXT NOT NULL,
  resolved_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_date     ON decisions(decision_date);
CREATE INDEX IF NOT EXISTS idx_decisions_county   ON decisions(county);
CREATE INDEX IF NOT EXISTS idx_decisions_type     ON decisions(development_type_id);
CREATE INDEX IF NOT EXISTS idx_decisions_outcome  ON decisions(decision_outcome);
CREATE INDEX IF NOT EXISTS idx_decisions_abp_ref  ON decisions(abp_reference);
CREATE INDEX IF NOT EXISTS idx_reasons_case       ON refusal_reasons(case_id_url);
CREATE INDEX IF NOT EXISTS idx_documents_case     ON documents(case_id_url);

CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
  case_id_url UNINDEXED,
  abp_reference UNINDEXED,
  site_address,
  reasons_concat,
  content='',
  tokenize='porter unicode61'
);
