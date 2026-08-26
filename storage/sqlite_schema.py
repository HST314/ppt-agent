PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    branch TEXT NOT NULL,
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    parent_checkpoint_id TEXT REFERENCES checkpoints(checkpoint_id),
    branch TEXT NOT NULL,
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checkpoints_parent_idx ON checkpoints(parent_checkpoint_id);
CREATE INDEX IF NOT EXISTS checkpoints_branch_time_idx ON checkpoints(branch, updated_at);
CREATE TABLE IF NOT EXISTS branches (
    name TEXT PRIMARY KEY,
    head_checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
    parent_branch TEXT,
    from_checkpoint_id TEXT,
    created_at TEXT,
    mode TEXT NOT NULL DEFAULT 'fork_after',
    source_stage TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time_idx ON events(at, event_id);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    sanitizer_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_revisions (
    revision_hash TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    document_type TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision_hash TEXT,
    markdown_body TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_revision_type_idx ON document_revisions(document_type, revision);
CREATE TABLE IF NOT EXISTS sample_revisions (
    revision_hash TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision_hash TEXT,
    feedback TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sample_revision_number_idx ON sample_revisions(revision);
CREATE TABLE IF NOT EXISTS sample_pages (
    revision_hash TEXT NOT NULL REFERENCES sample_revisions(revision_hash) ON DELETE CASCADE,
    page_index INTEGER NOT NULL,
    page_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sanitizer_version TEXT NOT NULL,
    PRIMARY KEY (revision_hash, page_index)
);
CREATE TABLE IF NOT EXISTS sample_packages (
    revision_hash TEXT PRIMARY KEY REFERENCES sample_revisions(revision_hash) ON DELETE CASCADE,
    package_hash TEXT NOT NULL,
    entrypoint TEXT NOT NULL,
    title TEXT NOT NULL,
    slide_count INTEGER NOT NULL,
    slides_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sample_package_files (
    revision_hash TEXT NOT NULL REFERENCES sample_packages(revision_hash) ON DELETE CASCADE,
    file_index INTEGER NOT NULL,
    logical_path TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (revision_hash, logical_path),
    UNIQUE (revision_hash, file_index)
);
CREATE TABLE IF NOT EXISTS full_deck_revisions (
    revision_hash TEXT PRIMARY KEY,
    full_deck_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision_hash TEXT,
    feedback TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS full_deck_revision_number_idx
ON full_deck_revisions(full_deck_id, revision);
CREATE TABLE IF NOT EXISTS full_deck_pages (
    revision_hash TEXT NOT NULL REFERENCES full_deck_revisions(revision_hash) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    slot_id TEXT NOT NULL,
    source_slide_number INTEGER,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    source_type TEXT NOT NULL,
    content_ref_json TEXT,
    derived_from_json TEXT,
    PRIMARY KEY (revision_hash, slot_id),
    UNIQUE (revision_hash, position)
);
CREATE TABLE IF NOT EXISTS full_deck_packages (
    revision_hash TEXT PRIMARY KEY REFERENCES full_deck_revisions(revision_hash) ON DELETE CASCADE,
    package_hash TEXT NOT NULL,
    entrypoint TEXT NOT NULL,
    title TEXT NOT NULL,
    slide_count INTEGER NOT NULL,
    slides_json TEXT NOT NULL,
    composition_manifest_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS full_deck_package_files (
    revision_hash TEXT NOT NULL REFERENCES full_deck_packages(revision_hash) ON DELETE CASCADE,
    file_index INTEGER NOT NULL,
    logical_path TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (revision_hash, logical_path),
    UNIQUE (revision_hash, file_index)
);
CREATE TABLE IF NOT EXISTS full_deck_generation_sessions (
    session_id TEXT PRIMARY KEY,
    full_deck_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_checkpoint_id TEXT NOT NULL,
    base_revision_hash TEXT NOT NULL,
    outline_revision_hash TEXT NOT NULL,
    sample_revision_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    planner_version TEXT NOT NULL,
    total_batches INTEGER NOT NULL,
    completed_batches INTEGER NOT NULL DEFAULT 0,
    active_batch_index INTEGER,
    session_version INTEGER NOT NULL DEFAULT 1,
    latest_preview_package_id TEXT,
    published_revision_hash TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS full_deck_generation_session_deck_idx
ON full_deck_generation_sessions(full_deck_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS full_deck_generation_active_session_idx
ON full_deck_generation_sessions(full_deck_id, branch)
WHERE status IN ('queued', 'running', 'pause_requested', 'paused', 'failed', 'finalizing');
CREATE TABLE IF NOT EXISTS full_deck_generation_batches (
    session_id TEXT NOT NULL
        REFERENCES full_deck_generation_sessions(session_id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    slot_ids_json TEXT NOT NULL,
    source_slide_numbers_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    segment_package_id TEXT,
    prompt_call_ids_json TEXT NOT NULL DEFAULT '[]',
    applied_directive_ids_json TEXT NOT NULL DEFAULT '[]',
    error_json TEXT,
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (session_id, batch_index)
);
CREATE TABLE IF NOT EXISTS full_deck_generation_pages (
    session_id TEXT NOT NULL
        REFERENCES full_deck_generation_sessions(session_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    slot_id TEXT NOT NULL,
    source_slide_number INTEGER,
    title TEXT NOT NULL,
    generation_status TEXT NOT NULL,
    batch_index INTEGER,
    source_type TEXT NOT NULL,
    content_ref_json TEXT,
    error_json TEXT,
    PRIMARY KEY (session_id, slot_id),
    UNIQUE (session_id, position)
);
CREATE INDEX IF NOT EXISTS full_deck_generation_pages_batch_idx
ON full_deck_generation_pages(session_id, batch_index, position);
CREATE TABLE IF NOT EXISTS full_deck_generation_directives (
    directive_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
        REFERENCES full_deck_generation_sessions(session_id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    apply_from_batch_index INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    first_applied_at TEXT
);
CREATE INDEX IF NOT EXISTS full_deck_generation_directives_session_idx
ON full_deck_generation_directives(session_id, created_at, directive_id);
CREATE TABLE IF NOT EXISTS full_deck_generation_packages (
    package_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
        REFERENCES full_deck_generation_sessions(session_id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    entrypoint TEXT NOT NULL,
    title TEXT NOT NULL,
    slide_count INTEGER NOT NULL,
    slides_json TEXT NOT NULL,
    composition_manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS full_deck_generation_packages_session_idx
ON full_deck_generation_packages(session_id, batch_index, kind, created_at);
CREATE TABLE IF NOT EXISTS full_deck_generation_package_files (
    package_id TEXT NOT NULL
        REFERENCES full_deck_generation_packages(package_id) ON DELETE CASCADE,
    file_index INTEGER NOT NULL,
    logical_path TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (package_id, logical_path),
    UNIQUE (package_id, file_index)
);
CREATE TABLE IF NOT EXISTS prompt_calls (
    prompt_call_id TEXT PRIMARY KEY,
    parent_prompt_call_id TEXT,
    project_id TEXT NOT NULL,
    state TEXT NOT NULL,
    status TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    template_id TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    template_hash TEXT NOT NULL,
    model_config_hash TEXT NOT NULL,
    runtime_config_hash TEXT NOT NULL,
    skills_hash TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    tool_calls_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_json TEXT,
    output_ref TEXT,
    output_hash TEXT
);
CREATE INDEX IF NOT EXISTS prompt_calls_started_idx ON prompt_calls(started_at, prompt_call_id);
CREATE TABLE IF NOT EXISTS prompt_call_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_call_id TEXT NOT NULL REFERENCES prompt_calls(prompt_call_id),
    at TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL
);
"""
