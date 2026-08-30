-- Empty production schema generated from commit
-- 63701875efd2f3846812bc9c64f6539b0aa381e0, immediately before migration
-- 0014_chat_run_goal_plan. This is deliberately frozen upgrade input: never
-- regenerate it from current ORM metadata, because doing so would hide a
-- missing ALTER TABLE migration behind create_all().
/* WARNING: Script requires that SQLITE_DBCONFIG_DEFENSIVE be disabled */
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE owner (
	id INTEGER NOT NULL,
	username VARCHAR(64) NOT NULL,
	hashed_password VARCHAR(255) NOT NULL,
	sso_subject VARCHAR(128),
	sso_email VARCHAR(320),
	provider VARCHAR(32) NOT NULL,
	auto_resume_on_limit_default BOOLEAN DEFAULT 0 NOT NULL,
	auto_resume_on_restart_default BOOLEAN DEFAULT 1 NOT NULL,
	model_prefs_json JSON,
	walkthrough_completed_at DATETIME,
	token_epoch INTEGER NOT NULL,
	created_at DATETIME,
	PRIMARY KEY (id),
	UNIQUE (username)
);
CREATE TABLE system_prompt_snapshots (
	id VARCHAR(64) NOT NULL,
	content TEXT NOT NULL,
	created_at DATETIME,
	PRIMARY KEY (id)
);
CREATE TABLE apps (
	id INTEGER NOT NULL,
	name VARCHAR(128) NOT NULL,
	description TEXT NOT NULL,
	jsx_source TEXT NOT NULL,
	compiled_path VARCHAR(512) NOT NULL,
	slug VARCHAR(128) NOT NULL,
	token_nonce VARCHAR(32),
	manifest_url VARCHAR(1024),
	published_manifest_url VARCHAR(1024),
	public_name VARCHAR(255),
	public_bundle_path VARCHAR(512),
	public_bundle_digest VARCHAR(64),
	public_source_commit VARCHAR(64),
	public_access_contract JSON,
	public_access_digest VARCHAR(64),
	public_token_nonce VARCHAR(32),
	public_published_at DATETIME,
	deleted_at DATETIME,
	version VARCHAR(32),
	theme_color VARCHAR(16),
	background_color VARCHAR(16),
	display VARCHAR(16),
	icon_png BLOB,
	icon_override_png BLOB,
	source_dir VARCHAR(512) NOT NULL,
	chat_id VARCHAR(64),
	pinned_at DATETIME,
	cross_app_access VARCHAR(16) NOT NULL,
	share_with_apps VARCHAR(16) NOT NULL,
	manage_apps BOOLEAN NOT NULL,
	manage_skills BOOLEAN NOT NULL,
	github_access BOOLEAN NOT NULL,
	github_connect BOOLEAN NOT NULL,
	filesystem_access BOOLEAN NOT NULL,
	connections_manage BOOLEAN NOT NULL,
	offline_capable BOOLEAN NOT NULL,
	embeds_agent BOOLEAN NOT NULL,
	chat_log_access VARCHAR(24) NOT NULL,
	upstream_commit VARCHAR(64),
	source_commit VARCHAR(64),
	conflict_resolver_chat_id VARCHAR(64),
	conflict_resolver_upstream_commit VARCHAR(64),
	upstream_jsx_sha VARCHAR(64),
	offline_contract JSON,
	system_prompt_file VARCHAR(255),
	system_app BOOLEAN NOT NULL,
	capability_contract JSON,
	created_at DATETIME,
	updated_at DATETIME, icon_ownership_split BOOLEAN NOT NULL DEFAULT FALSE,
	PRIMARY KEY (id),
	CONSTRAINT ck_apps_slug_nonempty CHECK (length(trim(slug)) > 0),
	CONSTRAINT ck_apps_source_dir_nonempty CHECK (length(trim(source_dir)) > 0)
);
CREATE TABLE connectors (
	id INTEGER NOT NULL,
	capability_id VARCHAR(64) NOT NULL,
	slug VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	url VARCHAR(2048) NOT NULL,
	auth_header VARCHAR(64),
	auth_value_encrypted TEXT,
	enabled BOOLEAN DEFAULT 1 NOT NULL,
	tools_json JSON NOT NULL,
	est_tokens INTEGER DEFAULT '0' NOT NULL,
	status VARCHAR(16) DEFAULT 'ok' NOT NULL,
	status_detail TEXT,
	created_at DATETIME,
	last_checked_at DATETIME,
	PRIMARY KEY (id)
);
CREATE TABLE oauth_client_registrations (
	issuer VARCHAR(512) NOT NULL,
	mode VARCHAR(16) NOT NULL,
	client_id VARCHAR(512) NOT NULL,
	client_secret_encrypted TEXT,
	registered_at DATETIME,
	PRIMARY KEY (issuer)
);
CREATE TABLE chats (
	id VARCHAR(64) NOT NULL,
	title VARCHAR(256) NOT NULL,
	title_locked BOOLEAN NOT NULL,
	messages JSON NOT NULL,
	has_messages BOOLEAN DEFAULT 0 NOT NULL,
	live_assistant JSON,
	pending_question_id VARCHAR(64),
	pending_messages JSON NOT NULL,
	uploads JSON NOT NULL,
	deleted_at DATETIME,
	session_id VARCHAR(128),
	provider VARCHAR(32) NOT NULL,
	agent_settings_json JSON,
	system_prompt_snapshot_id VARCHAR(64),
	auto_resume_on_limit BOOLEAN DEFAULT 0 NOT NULL,
	auto_resume_on_restart BOOLEAN DEFAULT 1 NOT NULL,
	pinned_at DATETIME,
	created_by_app_id INTEGER,
	created_at DATETIME,
	updated_at DATETIME,
	activity_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by_app_id) REFERENCES apps (id)
);
CREATE TABLE install_pass_grants (
	id INTEGER NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	app_id INTEGER NOT NULL,
	owner_epoch INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	consumed_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(app_id) REFERENCES apps (id)
);
CREATE TABLE app_activity_state (
	app_id INTEGER NOT NULL,
	activity_at DATETIME NOT NULL,
	activity_version INTEGER DEFAULT '1' NOT NULL,
	unseen BOOLEAN DEFAULT 1 NOT NULL,
	PRIMARY KEY (app_id),
	FOREIGN KEY(app_id) REFERENCES apps (id)
);
CREATE TABLE app_recency_state (
	app_id INTEGER NOT NULL,
	last_opened_at DATETIME NOT NULL,
	PRIMARY KEY (app_id),
	FOREIGN KEY(app_id) REFERENCES apps (id)
);
CREATE TABLE app_preview_state (
	app_id INTEGER NOT NULL,
	seen_updated_at DATETIME NOT NULL,
	seen_as_final BOOLEAN DEFAULT 0 NOT NULL,
	PRIMARY KEY (app_id),
	FOREIGN KEY(app_id) REFERENCES apps (id)
);
CREATE TABLE push_subscriptions (
	id VARCHAR(64) NOT NULL,
	owner_id INTEGER NOT NULL,
	endpoint TEXT NOT NULL,
	p256dh TEXT NOT NULL,
	auth TEXT NOT NULL,
	created_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES owner (id),
	UNIQUE (endpoint)
);
CREATE TABLE notifications (
	id VARCHAR(64) NOT NULL,
	owner_id INTEGER NOT NULL,
	source_type VARCHAR(16) NOT NULL,
	source_id VARCHAR(64),
	title VARCHAR(256) NOT NULL,
	body TEXT,
	icon TEXT,
	target TEXT,
	actions JSON,
	sent_at DATETIME,
	clicked_at DATETIME,
	read_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES owner (id)
);
CREATE TABLE connector_oauth (
	connector_id INTEGER NOT NULL,
	resource VARCHAR(2048) NOT NULL,
	issuer VARCHAR(512) NOT NULL,
	authorization_endpoint VARCHAR(2048) NOT NULL,
	token_endpoint VARCHAR(2048) NOT NULL,
	registration_endpoint VARCHAR(2048),
	revocation_endpoint VARCHAR(2048),
	scopes_advertised JSON NOT NULL,
	access_token_encrypted TEXT,
	refresh_token_encrypted TEXT,
	access_expires_at DATETIME,
	scopes_granted JSON NOT NULL,
	connected_at DATETIME,
	auth_mode VARCHAR(16) DEFAULT 'browser' NOT NULL,
	client_id VARCHAR(512),
	client_secret_encrypted TEXT,
	user_project VARCHAR(256),
	PRIMARY KEY (connector_id),
	FOREIGN KEY(connector_id) REFERENCES connectors (id) ON DELETE CASCADE
);
CREATE TABLE chat_runs (
	id VARCHAR(64) NOT NULL,
	root_run_id VARCHAR(64),
	chat_id VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL,
	provider VARCHAR(32),
	goal_objective TEXT,
	initiated_by_app_id INTEGER,
	cost_usd FLOAT,
	provider_session_id VARCHAR(128),
	input_tokens INTEGER,
	output_tokens INTEGER,
	cache_read_input_tokens INTEGER,
	cache_creation_input_tokens INTEGER,
	reasoning_output_tokens INTEGER,
	total_tokens INTEGER,
	model_context_window INTEGER,
	usage_json JSON,
	started_at DATETIME,
	ended_at DATETIME,
	parked_until DATETIME,
	park_reason VARCHAR(32),
	restart_nonce VARCHAR(128),
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(initiated_by_app_id) REFERENCES apps (id)
);
CREATE TABLE delegations (
	id VARCHAR(64) NOT NULL,
	app_id INTEGER NOT NULL,
	parent_chat_id VARCHAR(64) NOT NULL,
	parent_root_run_id VARCHAR(64) NOT NULL,
	task_key VARCHAR(128) NOT NULL,
	child_chat_id VARCHAR(64) NOT NULL,
	provider VARCHAR(32) NOT NULL,
	model VARCHAR(256),
	effort VARCHAR(32),
	scope VARCHAR(16) NOT NULL,
	cwd VARCHAR(1024) NOT NULL,
	prompt_sha256 VARCHAR(64) NOT NULL,
	max_budget_usd FLOAT,
	created_at DATETIME NOT NULL,
	cancelled_at DATETIME,
	notify_parent_on_complete BOOLEAN NOT NULL,
	parent_woken_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_delegations_parent_root_task UNIQUE (parent_root_run_id, task_key),
	FOREIGN KEY(app_id) REFERENCES apps (id),
	FOREIGN KEY(parent_chat_id) REFERENCES chats (id),
	FOREIGN KEY(child_chat_id) REFERENCES chats (id)
);
CREATE TABLE chat_session_links (
	provider VARCHAR(32) NOT NULL,
	session_id VARCHAR(128) NOT NULL,
	chat_id VARCHAR(64) NOT NULL,
	first_seen_at DATETIME,
	last_seen_at DATETIME,
	PRIMARY KEY (provider, session_id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
);
CREATE TABLE agent_lifecycle_run_updates (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
	chat_id VARCHAR(64) NOT NULL,
	chat_run_id VARCHAR(64) NOT NULL,
	provider VARCHAR(32),
	status VARCHAR(16) NOT NULL,
	started_at DATETIME,
	ended_at DATETIME,
	observed_at DATETIME NOT NULL,
	FOREIGN KEY(chat_id) REFERENCES chats (id)
);
CREATE TABLE chat_embed_grants (
	id INTEGER NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	app_id INTEGER NOT NULL,
	app_nonce VARCHAR(64) NOT NULL,
	chat_id VARCHAR(64) NOT NULL,
	instance_id VARCHAR(160) NOT NULL,
	owner_epoch INTEGER NOT NULL,
	role VARCHAR(32) NOT NULL,
	operations_json JSON NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	consumed_at DATETIME,
	session_id VARCHAR(64),
	session_expires_at DATETIME,
	revoked_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(app_id) REFERENCES apps (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
);
CREATE TABLE tool_outputs (
	chat_id VARCHAR(64) NOT NULL,
	tool_use_id VARCHAR(128) NOT NULL,
	output TEXT NOT NULL,
	created_at DATETIME,
	PRIMARY KEY (chat_id, tool_use_id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
);
CREATE TABLE thinking_traces (
	chat_id VARCHAR(64) NOT NULL,
	thinking_id VARCHAR(128) NOT NULL,
	content TEXT NOT NULL,
	revision INTEGER NOT NULL,
	complete BOOLEAN NOT NULL,
	created_at DATETIME,
	PRIMARY KEY (chat_id, thinking_id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
);
CREATE TABLE contribution_autopilot (
	app_id INTEGER NOT NULL,
	record_id VARCHAR(128) NOT NULL,
	enabled BOOLEAN NOT NULL,
	granted_at DATETIME,
	granted_head_sha VARCHAR(64),
	target_repo VARCHAR(256),
	target_pr_number INTEGER,
	target_head_repository VARCHAR(256),
	target_branch VARCHAR(256),
	target_repo_path VARCHAR(1024),
	state VARCHAR(16) NOT NULL,
	run_id VARCHAR(64),
	attention_key VARCHAR(256),
	claimed_event_at VARCHAR(40),
	round_action VARCHAR(16),
	round_head_sha VARCHAR(64),
	ignored_event_urls_json JSON,
	claimed_at DATETIME,
	lease_expires_at DATETIME,
	followup_chat_id VARCHAR(64),
	rounds_used INTEGER NOT NULL,
	max_rounds INTEGER NOT NULL,
	consecutive_failures INTEGER NOT NULL,
	last_handled_event_at VARCHAR(40),
	last_handled_attention_key VARCHAR(256),
	rounds_json JSON,
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (app_id, record_id),
	FOREIGN KEY(app_id) REFERENCES apps (id),
	FOREIGN KEY(followup_chat_id) REFERENCES chats (id)
);
CREATE TABLE agent_lifecycle_events (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
	event_key VARCHAR(64) NOT NULL,
	chat_id VARCHAR(64) NOT NULL,
	chat_run_id VARCHAR(64),
	provider VARCHAR(32) NOT NULL,
	provider_session_id VARCHAR(160),
	provider_agent_id VARCHAR(160) NOT NULL,
	agent_id VARCHAR(70) NOT NULL,
	activation_id VARCHAR(75) NOT NULL,
	parent_agent_id VARCHAR(70),
	parent_activation_id VARCHAR(75),
	parent_kind VARCHAR(16) NOT NULL,
	parent_source_id VARCHAR(160),
	event_type VARCHAR(32) NOT NULL,
	state VARCHAR(16) NOT NULL,
	agent_type VARCHAR(64),
	summary TEXT,
	occurred_at DATETIME,
	observed_at DATETIME NOT NULL,
	time_quality VARCHAR(16) NOT NULL,
	source VARCHAR(32) NOT NULL,
	source_event_id VARCHAR(160),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(chat_run_id) REFERENCES chat_runs (id)
);
CREATE TABLE schema_migrations (version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL);
INSERT INTO schema_migrations VALUES('0001_legacy_schema_convergence','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0002_chat_run_goal_objective','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0003_chat_run_root_identity','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0004_app_identity_required','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0005_connectors','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0006_connector_capability_identity','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0007_chat_has_messages','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0008_chat_search_documents','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0009_app_connections_manage','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0010_chat_pending_question_id','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0011_delegation_parent_wake','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0012_connector_oauth_gcloud','2026-08-15 00:00:00');
INSERT INTO schema_migrations VALUES('0013_app_hosted_publication','2026-08-15 00:00:00');
CREATE TABLE chat_search_docs (id INTEGER PRIMARY KEY, chat_id VARCHAR(64) NOT NULL, msg_idx INTEGER NOT NULL, ts BIGINT, role VARCHAR(16), text TEXT NOT NULL);
CREATE TABLE chat_search_state (chat_id VARCHAR(64) PRIMARY KEY, indexed_updated_at TEXT NOT NULL);
PRAGMA writable_schema=ON;
INSERT INTO sqlite_schema(type,name,tbl_name,rootpage,sql)VALUES('table','chat_search_fts','chat_search_fts',0,'CREATE VIRTUAL TABLE chat_search_fts USING fts5(text, content=''chat_search_docs'', content_rowid=''id'', tokenize=''unicode61 remove_diacritics 2'')');
CREATE TABLE IF NOT EXISTS 'chat_search_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
INSERT INTO chat_search_fts_data VALUES(1,X'');
INSERT INTO chat_search_fts_data VALUES(10,X'00000000000000');
CREATE TABLE IF NOT EXISTS 'chat_search_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS 'chat_search_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE IF NOT EXISTS 'chat_search_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
INSERT INTO chat_search_fts_config VALUES('version',4);
DELETE FROM sqlite_sequence;
CREATE TRIGGER apps_require_identity_insert BEFORE INSERT ON apps WHEN NEW.slug IS NULL OR length(trim(NEW.slug)) = 0 OR NEW.source_dir IS NULL OR length(trim(NEW.source_dir)) = 0 BEGIN SELECT RAISE(ABORT, 'apps require slug and source_dir'); END;
CREATE TRIGGER apps_require_identity_update BEFORE UPDATE OF slug, source_dir ON apps WHEN NEW.slug IS NULL OR length(trim(NEW.slug)) = 0 OR NEW.source_dir IS NULL OR length(trim(NEW.source_dir)) = 0 BEGIN SELECT RAISE(ABORT, 'apps require slug and source_dir'); END;
CREATE TRIGGER connectors_require_capability_insert BEFORE INSERT ON connectors WHEN NEW.capability_id IS NULL OR length(trim(NEW.capability_id)) = 0 BEGIN SELECT RAISE(ABORT, 'connectors require capability_id'); END;
CREATE TRIGGER connectors_require_capability_update BEFORE UPDATE OF capability_id ON connectors WHEN NEW.capability_id IS NULL OR length(trim(NEW.capability_id)) = 0 BEGIN SELECT RAISE(ABORT, 'connectors require capability_id'); END;
CREATE TRIGGER chat_search_docs_ai AFTER INSERT ON chat_search_docs BEGIN INSERT INTO chat_search_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER chat_search_docs_ad AFTER DELETE ON chat_search_docs BEGIN INSERT INTO chat_search_fts(chat_search_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
CREATE UNIQUE INDEX ix_apps_slug ON apps (slug);
CREATE INDEX ix_apps_id ON apps (id);
CREATE INDEX ix_apps_manifest_url ON apps (manifest_url);
CREATE UNIQUE INDEX ix_apps_source_dir ON apps (source_dir);
CREATE UNIQUE INDEX ix_connectors_capability_id ON connectors (capability_id);
CREATE UNIQUE INDEX ix_connectors_slug ON connectors (slug);
CREATE INDEX ix_connectors_id ON connectors (id);
CREATE UNIQUE INDEX ix_install_pass_grants_token_hash ON install_pass_grants (token_hash);
CREATE INDEX ix_install_pass_grants_consumed_at ON install_pass_grants (consumed_at);
CREATE INDEX ix_install_pass_grants_app_id ON install_pass_grants (app_id);
CREATE INDEX ix_install_pass_grants_expires_at ON install_pass_grants (expires_at);
CREATE INDEX ix_chat_runs_status ON chat_runs (status);
CREATE INDEX ix_chat_runs_chat_id ON chat_runs (chat_id);
CREATE INDEX ix_chat_runs_root_run_id ON chat_runs (root_run_id);
CREATE INDEX ix_delegations_parent_root_run_id ON delegations (parent_root_run_id);
CREATE INDEX ix_delegations_parent_chat_id ON delegations (parent_chat_id);
CREATE UNIQUE INDEX ix_delegations_child_chat_id ON delegations (child_chat_id);
CREATE INDEX ix_delegations_app_id ON delegations (app_id);
CREATE INDEX ix_chat_session_links_chat_id ON chat_session_links (chat_id);
CREATE INDEX ix_agent_lifecycle_run_updates_chat_id ON agent_lifecycle_run_updates (chat_id);
CREATE INDEX ix_agent_lifecycle_run_updates_chat_run_id ON agent_lifecycle_run_updates (chat_run_id);
CREATE INDEX ix_chat_embed_grants_expires_at ON chat_embed_grants (expires_at);
CREATE INDEX ix_chat_embed_grants_revoked_at ON chat_embed_grants (revoked_at);
CREATE INDEX ix_chat_embed_grants_instance_id ON chat_embed_grants (instance_id);
CREATE UNIQUE INDEX ix_chat_embed_grants_token_hash ON chat_embed_grants (token_hash);
CREATE INDEX ix_chat_embed_grants_app_id ON chat_embed_grants (app_id);
CREATE UNIQUE INDEX ix_chat_embed_grants_session_id ON chat_embed_grants (session_id);
CREATE INDEX ix_chat_embed_grants_session_expires_at ON chat_embed_grants (session_expires_at);
CREATE INDEX ix_chat_embed_grants_chat_id ON chat_embed_grants (chat_id);
CREATE INDEX ix_tool_outputs_chat_id ON tool_outputs (chat_id);
CREATE INDEX ix_thinking_traces_chat_id ON thinking_traces (chat_id);
CREATE INDEX ix_agent_lifecycle_events_activation_id ON agent_lifecycle_events (activation_id);
CREATE UNIQUE INDEX ix_agent_lifecycle_events_event_key ON agent_lifecycle_events (event_key);
CREATE INDEX ix_agent_lifecycle_events_parent_agent_id ON agent_lifecycle_events (parent_agent_id);
CREATE INDEX ix_agent_lifecycle_events_chat_id ON agent_lifecycle_events (chat_id);
CREATE INDEX ix_agent_lifecycle_events_agent_id ON agent_lifecycle_events (agent_id);
CREATE INDEX ix_agent_lifecycle_events_chat_run_id ON agent_lifecycle_events (chat_run_id);
CREATE INDEX ix_agent_lifecycle_events_parent_activation_id ON agent_lifecycle_events (parent_activation_id);
CREATE UNIQUE INDEX ix_chat_search_docs_chat_message ON chat_search_docs (chat_id, msg_idx);
PRAGMA writable_schema=OFF;
COMMIT;
