"""Numbered SQLite upgrades; version 4 is the first supported archive format.

Keep BASE_SCHEMA immutable. Add each upgrade under its source version and bump
SCHEMA_VERSION. Statements run in one transaction; do not use executescript,
commit, or transaction-control statements inside migrations.
"""

BASE_SCHEMA_VERSION = 4
SCHEMA_VERSION = 8

BASE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS collections (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        relative_path TEXT NOT NULL UNIQUE,
        input_directory TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pages (
        id INTEGER PRIMARY KEY,
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (collection_id, filename)
    )""",
    """CREATE TABLE IF NOT EXISTS page_order (
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK (position > 0),
        PRIMARY KEY (collection_id, page_id),
        UNIQUE (collection_id, position)
    )""",
    """CREATE TABLE IF NOT EXISTS analysis_runs (
        id INTEGER PRIMARY KEY,
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        model TEXT NOT NULL,
        page_analysis_key TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS prompt_versions (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (kind, content_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS run_prompts (
        run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        prompt_version_id INTEGER NOT NULL REFERENCES prompt_versions(id),
        PRIMARY KEY (run_id, kind)
    )""",
    """CREATE TABLE IF NOT EXISTS page_analyses (
        id INTEGER PRIMARY KEY,
        page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        model TEXT NOT NULL,
        analysis_key TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS run_pages (
        run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        page_id INTEGER NOT NULL REFERENCES pages(id),
        analysis_id INTEGER NOT NULL REFERENCES page_analyses(id),
        position INTEGER NOT NULL CHECK (position > 0),
        PRIMARY KEY (run_id, page_id),
        UNIQUE (run_id, position)
    )""",
    """CREATE TABLE IF NOT EXISTS boundaries (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        left_page_id INTEGER NOT NULL REFERENCES pages(id),
        right_page_id INTEGER NOT NULL REFERENCES pages(id),
        right_position INTEGER NOT NULL CHECK (right_position > 1),
        starts_new_document INTEGER NOT NULL CHECK (starts_new_document IN (0, 1)),
        confidence REAL NOT NULL,
        reason TEXT NOT NULL,
        UNIQUE (run_id, right_position),
        UNIQUE (run_id, left_page_id, right_page_id)
    )""",
    """CREATE TABLE IF NOT EXISTS boundary_overrides (
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        left_page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        right_page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        decision INTEGER NOT NULL CHECK (decision IN (0, 1)),
        reviewed_at TEXT NOT NULL,
        PRIMARY KEY (collection_id, left_page_id, right_page_id)
    )""",
    """CREATE TABLE IF NOT EXISTS review_page_order (
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK (position > 0),
        PRIMARY KEY (collection_id, page_id),
        UNIQUE (collection_id, position)
    )""",
    """CREATE TABLE IF NOT EXISTS review_history (
        id INTEGER PRIMARY KEY,
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        page_order_json TEXT NOT NULL,
        boundary_overrides_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reviewed_labels (
        id INTEGER PRIMARY KEY,
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        page_ids_json TEXT NOT NULL,
        label TEXT NOT NULL,
        document_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        date TEXT NOT NULL,
        places_json TEXT NOT NULL,
        subjects_json TEXT NOT NULL,
        confidence REAL NOT NULL,
        reviewed_at TEXT NOT NULL,
        UNIQUE (collection_id, page_ids_json)
    )""",
    """CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        document_number INTEGER NOT NULL,
        label TEXT NOT NULL,
        document_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        date TEXT NOT NULL,
        places_json TEXT NOT NULL,
        subjects_json TEXT NOT NULL,
        label_confidence REAL NOT NULL,
        lowest_boundary_confidence REAL NOT NULL,
        UNIQUE (run_id, document_number)
    )""",
    """CREATE TABLE IF NOT EXISTS document_pages (
        document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        page_id INTEGER NOT NULL REFERENCES pages(id),
        position INTEGER NOT NULL CHECK (position > 0),
        PRIMARY KEY (document_id, page_id),
        UNIQUE (document_id, position)
    )""",
    """CREATE INDEX IF NOT EXISTS page_order_position
        ON page_order(collection_id, position)""",
    """CREATE INDEX IF NOT EXISTS page_analyses_lookup
        ON page_analyses(page_id, analysis_key, id DESC)""",
    """CREATE INDEX IF NOT EXISTS completed_runs
        ON analysis_runs(collection_id, completed_at, id DESC)""",
)

# Source version -> statements to reach the next version.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    4: (
        """CREATE TABLE boundary_cache (
            cache_key TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
    ),
    5: (
        """CREATE TABLE run_sources (
            run_id INTEGER PRIMARY KEY REFERENCES analysis_runs(id) ON DELETE CASCADE,
            manifest_json TEXT NOT NULL,
            request_json TEXT NOT NULL
        )""",
        """CREATE TABLE analysis_sources (
            analysis_id INTEGER PRIMARY KEY REFERENCES page_analyses(id) ON DELETE CASCADE,
            sha256 TEXT NOT NULL
        )""",
        """CREATE TABLE stage_results (
            run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            input_key TEXT NOT NULL,
            model TEXT NOT NULL,
            result_json TEXT NOT NULL,
            PRIMARY KEY (run_id, stage, input_key)
        )""",
    ),

    6: (
        """CREATE TABLE document_flags (
            collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            flagged_at TEXT NOT NULL,
            PRIMARY KEY (collection_id, page_id)
        )""",
    ),
    7: (
        """CREATE TABLE edit_history (
            id INTEGER PRIMARY KEY,
            collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            reviewer_name TEXT NOT NULL CHECK (length(trim(reviewer_name)) > 0),
            action TEXT NOT NULL,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            imported INTEGER NOT NULL DEFAULT 0 CHECK (imported IN (0, 1))
        )""",
        """CREATE INDEX edit_history_collection ON edit_history(collection_id, id DESC)""",
        """INSERT INTO edit_history(
            collection_id, reviewer_name, action, summary, details_json, recorded_at, imported
        ) SELECT c.id, 'John Doe', 'existing_state', 'Existing saved edits at history activation',
            json_object(
                'boundaries', json((SELECT json_group_array(json_object(
                    'left_page_id', left_page_id, 'right_page_id', right_page_id,
                    'decision', decision, 'reviewed_at', reviewed_at
                )) FROM boundary_overrides WHERE collection_id = c.id)),
                'page_order', json((SELECT json_group_array(json_object(
                    'page_id', page_id, 'position', position
                )) FROM (SELECT page_id, position FROM review_page_order
                    WHERE collection_id = c.id ORDER BY position))),
                'labels', json((SELECT json_group_array(json_object(
                    'page_ids', json(page_ids_json), 'label', label, 'document_type', document_type,
                    'summary', summary, 'date', date, 'places', json(places_json),
                    'subjects', json(subjects_json), 'confidence', confidence, 'reviewed_at', reviewed_at
                )) FROM reviewed_labels WHERE collection_id = c.id)),
                'flags', json((SELECT json_group_array(json_object(
                    'page_id', page_id, 'flagged_at', flagged_at
                )) FROM document_flags WHERE collection_id = c.id)),
                'undo_snapshots', json((SELECT json_group_array(json_object(
                    'id', id, 'page_order', json(page_order_json),
                    'boundaries', json(boundary_overrides_json), 'created_at', created_at
                )) FROM review_history WHERE collection_id = c.id))
            ), strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), 1
        FROM collections c WHERE
            EXISTS (SELECT 1 FROM boundary_overrides WHERE collection_id = c.id) OR
            EXISTS (SELECT 1 FROM review_page_order WHERE collection_id = c.id) OR
            EXISTS (SELECT 1 FROM reviewed_labels WHERE collection_id = c.id) OR
            EXISTS (SELECT 1 FROM document_flags WHERE collection_id = c.id) OR
            EXISTS (SELECT 1 FROM review_history WHERE collection_id = c.id)""",
    ),
}
