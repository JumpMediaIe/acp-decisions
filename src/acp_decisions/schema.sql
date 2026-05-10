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
  -- Structured entities extracted by the classifier (Option B). All optional.
  summary                  TEXT,           -- one-sentence plain-English summary
  dev_plan                 TEXT,           -- e.g. "Dublin City Development Plan 2022-2028"
  policy_codes             TEXT,           -- JSON array: ["CPO 7.5", "Section 14.7.9"]
  quantitative_violation   TEXT,           -- e.g. "145 units exceeds Z29 zoning limit"
  statutory_test           TEXT,           -- e.g. "Habitats Directive Article 6"
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

-- Council-level planning applications (LGMA national dataset, fetched via ArcGIS REST).
-- Separate from `decisions` (which is ACP appeals only). This table is the
-- upstream source — local-authority decisions, ~492k total nationally,
-- ~50k of which are refusals.
CREATE TABLE IF NOT EXISTS planning_applications (
  object_id              INTEGER PRIMARY KEY,
  planning_authority     TEXT NOT NULL,         -- "Carlow County Council"
  application_number     TEXT NOT NULL,
  development_description TEXT,
  development_address    TEXT,
  development_postcode   TEXT,
  application_status     TEXT,
  application_type       TEXT,
  decision               TEXT,                  -- "REFUSED", "GRANTED", "GRANT WITH CONDITIONS", etc.
  land_use_code          TEXT,
  area_of_site           REAL,
  num_residential_units  INTEGER,
  one_off_house          TEXT,                  -- "Y"/"N"/blank — single-house indicator
  floor_area             REAL,
  received_date          TEXT,
  decision_date          TEXT,
  decision_due_date      TEXT,
  grant_date             TEXT,
  expiry_date            TEXT,
  appeal_ref_number      TEXT,                  -- e.g. "ABP305059-19" — links to ACP `decisions.abp_reference`
  appeal_status          TEXT,
  appeal_decision        TEXT,
  appeal_decision_date   TEXT,
  appeal_submitted_date  TEXT,
  link_app_details       TEXT,                  -- URL to the council's page for this application
  one_off_kpi            TEXT,
  itm_easting            REAL,
  itm_northing           REAL,
  fetched_at             TEXT NOT NULL
);

-- Refusal reasons fetched from council portals (currently agileapplications.ie).
-- Linked to planning_applications via object_id (the LGMA OBJECTID).
-- Same shape as `refusal_reasons` (ACP appeals) so the classifier can run
-- entity extraction on it identically.
CREATE TABLE IF NOT EXISTS council_refusal_reasons (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  object_id                INTEGER NOT NULL,
  reason_number            INTEGER NOT NULL,
  short_prescription       TEXT,
  raw_text                 TEXT NOT NULL,
  -- Same entity fields as refusal_reasons; populated by the classifier
  summary                  TEXT,
  dev_plan                 TEXT,
  policy_codes             TEXT,                -- JSON array as TEXT
  quantitative_violation   TEXT,
  statutory_test           TEXT,
  fetched_at               TEXT NOT NULL,
  classified_at            TEXT,
  FOREIGN KEY (object_id) REFERENCES planning_applications(object_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_crr_object_id ON council_refusal_reasons(object_id);

-- Many-to-many: council refusal reasons → taxonomy categories.
-- Mirrors `reason_categories` (which exists for ACP appeals) so analytics
-- queries can be uniform across both archives.
CREATE TABLE IF NOT EXISTS council_reason_categories (
  reason_id   INTEGER NOT NULL,
  category_id TEXT NOT NULL,
  PRIMARY KEY (reason_id, category_id),
  FOREIGN KEY (reason_id) REFERENCES council_refusal_reasons(id) ON DELETE CASCADE,
  FOREIGN KEY (category_id) REFERENCES categories(id)
);

-- Tracks council applications we've already attempted (success or empty) so the
-- scraper can resume incrementally without re-fetching every week.
CREATE TABLE IF NOT EXISTS council_reasons_fetch (
  object_id     INTEGER PRIMARY KEY,
  fetched_at    TEXT NOT NULL,
  reasons_count INTEGER NOT NULL,
  error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_pa_authority      ON planning_applications(planning_authority);
CREATE INDEX IF NOT EXISTS idx_pa_decision       ON planning_applications(decision);
CREATE INDEX IF NOT EXISTS idx_pa_decision_date  ON planning_applications(decision_date);
CREATE INDEX IF NOT EXISTS idx_pa_appeal_ref     ON planning_applications(appeal_ref_number);
CREATE INDEX IF NOT EXISTS idx_pa_one_off_house  ON planning_applications(one_off_house);

CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
  case_id_url UNINDEXED,
  abp_reference UNINDEXED,
  site_address,
  reasons_concat,
  content='',
  tokenize='porter unicode61'
);
