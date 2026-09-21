CREATE TABLE IF NOT EXISTS source_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_category TEXT NOT NULL,
  external_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  published_at TEXT,
  university_name TEXT,
  title TEXT NOT NULL,
  author TEXT,
  raw_text TEXT NOT NULL,
  document_id TEXT,
  policy_status TEXT,
  financial_cost REAL,
  funding_source TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  content_hash TEXT NOT NULL,
  screened_at TEXT,
  emailed_at TEXT,
  analysis TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS source_items_pending_idx
  ON source_items (observed_at)
  WHERE screened_at IS NULL;

CREATE TABLE IF NOT EXISTS monitor_runs (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
  details TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS digest_deliveries (
  idempotency_key TEXT PRIMARY KEY,
  external_ids TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inbound_messages (
  email_id TEXT PRIMARY KEY,
  sender TEXT NOT NULL,
  subject TEXT NOT NULL,
  message_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
  request_text TEXT NOT NULL,
  attachments TEXT NOT NULL DEFAULT '[]',
  result TEXT,
  delivery_id TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS inbound_messages_status_idx
  ON inbound_messages (status, received_at);
