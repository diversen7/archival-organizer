import sqlite3

import pytest

from archival_organizer import migrations, storage


@pytest.fixture
def version_four_archive(tmp_path):
    output = tmp_path / "archive"
    with storage.connect(output) as database:
        for statement in migrations.BASE_SCHEMA:
            database.execute(statement)
        database.execute("INSERT INTO metadata VALUES ('schema_version', '4')")
    storage.upsert_collection(output, "default", "Sample", ".", tmp_path)
    storage.sync_pages(output, "default", [tmp_path / "1.png", tmp_path / "2.png"])
    run_id = storage.start_run(output, "default", "model", "key", {"boundary": "Prompt"})
    for number in (1, 2):
        storage.save_page_analysis(
            output, "default", run_id,
            {"page": number, "file": f"{number}.png", "transcription": "Keep this OCR"},
            "model", "key",
        )
    storage.save_results(output, "default", [{
        "section": 1, "start_page": 1, "end_page": 2, "label": "Document",
        "document_type": "report", "summary": "", "date": "", "places": [],
        "subjects": [], "label_confidence": 0.9, "lowest_boundary_confidence": 0.8,
    }], {2: {
        "right_page": 2, "starts_new_document": False, "confidence": 0.8,
        "reason": "Continues",
    }}, run_id)
    storage.set_boundary_override(output, "default", 2, True)
    storage.save_reviewed_label(output, "default", [1], {
        "label": "Reviewed", "document_type": "report", "summary": "Keep this label",
        "date": "", "places": [], "subjects": [], "confidence": 0.9,
    })
    return output


def _dump(output):
    with sqlite3.connect(output / storage.DATABASE_NAME) as database:
        return "\n".join(database.iterdump())


def test_boundary_cache_migration_preserves_v4_data(version_four_archive):
    output = version_four_archive
    before = storage.load_collection_data(output, "default")
    storage.initialize(output)
    storage.save_boundary_batch(output, "key", [{"right_page": 2}])
    storage.initialize(output)

    assert storage.load_collection_data(output, "default") == before
    assert storage.load_cached_page(output, "default", 1, "1.png", "key") is not None
    assert storage.load_boundary_batch(output, "key") == [{"right_page": 2}]
    with storage.connect(output) as database:
        assert database.execute("SELECT value FROM metadata").fetchone()[0] == str(migrations.SCHEMA_VERSION)
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fresh_and_migrated_archives_have_identical_schema(version_four_archive, tmp_path):
    fresh = tmp_path / "fresh"
    storage.initialize(fresh)
    storage.initialize(version_four_archive)

    def schema(output):
        with storage.connect(output) as database:
            return [tuple(row) for row in database.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )]

    assert schema(fresh) == schema(version_four_archive)


def test_v5_upgrade_preserves_results_and_boundary_cache(version_four_archive):
    output = version_four_archive
    with storage.connect(output) as database:
        for statement in migrations.MIGRATIONS[4]:
            database.execute(statement)
        database.execute("UPDATE metadata SET value = '5' WHERE key = 'schema_version'")
    before = storage.load_collection_data(output, "default")
    storage.save_boundary_batch(output, "existing", [{"right_page": 2}])
    storage.initialize(output)
    assert storage.load_collection_data(output, "default") == before
    assert storage.load_boundary_batch(output, "existing") == [{"right_page": 2}]
    with storage.connect(output) as database:
        assert database.execute("SELECT COUNT(*) FROM run_sources").fetchone()[0] == 0
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_real_v6_migration_rolls_back_on_failure(version_four_archive, monkeypatch):
    output = version_four_archive
    with storage.connect(output) as database:
        for statement in migrations.MIGRATIONS[4]:
            database.execute(statement)
        database.execute("UPDATE metadata SET value = '5' WHERE key = 'schema_version'")
    before = _dump(output)
    monkeypatch.setitem(migrations.MIGRATIONS, 5, migrations.MIGRATIONS[5] + (
        "INSERT INTO nonexistent_table VALUES (1)",
    ))
    with pytest.raises(sqlite3.OperationalError, match="nonexistent_table"):
        storage.initialize(output)
    assert _dump(output) == before


def test_upgrade_preserves_archive_and_only_runs_once(version_four_archive, monkeypatch):
    output = version_four_archive
    before = storage.load_collection_data(output, "default")
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 5)
    monkeypatch.setitem(migrations.MIGRATIONS, 4, (
        "CREATE TABLE migration_example (id INTEGER PRIMARY KEY)",
        "INSERT INTO migration_example VALUES (1)",
    ))

    storage.initialize(output)
    storage.initialize(output)

    assert storage.load_collection_data(output, "default") == before
    assert storage.load_cached_page(output, "default", 1, "1.png", "key") is not None
    with storage.connect(output) as database:
        assert database.execute("SELECT * FROM migration_example").fetchone()[0] == 1
        assert database.execute("SELECT value FROM metadata").fetchone()[0] == "5"


def test_failed_upgrade_rolls_back_all_steps_and_version(version_four_archive, monkeypatch):
    output = version_four_archive
    before = _dump(output)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 6)
    monkeypatch.setitem(migrations.MIGRATIONS, 4, (
        "CREATE TABLE migration_example (id INTEGER PRIMARY KEY)",
        "UPDATE page_analyses SET result_json = '{}'",
    ))
    monkeypatch.setitem(migrations.MIGRATIONS, 5, (
        "INSERT INTO nonexistent_table VALUES (1)",
    ))

    with pytest.raises(sqlite3.OperationalError, match="nonexistent_table"):
        storage.initialize(output)

    assert _dump(output) == before


def test_missing_upgrade_leaves_archive_unchanged(version_four_archive, monkeypatch):
    output = version_four_archive
    before = _dump(output)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 5)
    monkeypatch.delitem(migrations.MIGRATIONS, 4, raising=False)
    with pytest.raises(ValueError, match="Missing database migration"):
        storage.initialize(output)
    assert _dump(output) == before


@pytest.mark.parametrize("version,message", [
    ("999", "newer than supported"),
    ("1", "migrations start at version 4"),
    ("invalid", "no valid schema version"),
    (None, "no valid schema version"),
])
def test_unsupported_versions_leave_archive_unchanged(tmp_path, version, message):
    with storage.connect(tmp_path) as database:
        database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        if version is not None:
            database.execute("INSERT INTO metadata VALUES ('schema_version', ?)", (version,))
    before = _dump(tmp_path)
    with pytest.raises(ValueError, match=message):
        storage.initialize(tmp_path)
    assert _dump(tmp_path) == before


def test_unversioned_database_is_not_adopted(tmp_path):
    with storage.connect(tmp_path) as database:
        database.execute("CREATE TABLE unrelated (value TEXT)")
    before = _dump(tmp_path)
    with pytest.raises(ValueError, match="no valid schema version"):
        storage.initialize(tmp_path)
    assert _dump(tmp_path) == before
