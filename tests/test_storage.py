from pathlib import Path
import sqlite3

import pytest

from archival_organizer import storage


def _setup(output: Path, source: Path) -> None:
    storage.initialize(output)
    storage.upsert_collection(output, "default", "Sample", ".", source)


def test_page_identity_survives_reordering(tmp_path: Path):
    output = tmp_path / "output"
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    _setup(output, tmp_path)

    storage.sync_pages(output, "default", [first, second])
    with storage.connect(output) as database:
        original = {
            row["filename"]: int(row["id"])
            for row in database.execute("SELECT id, filename FROM pages")
        }

    storage.sync_pages(output, "default", [second, first])
    with storage.connect(output) as database:
        reordered = {
            row["filename"]: (int(row["id"]), int(row["position"]))
            for row in database.execute(
                """SELECT p.id, p.filename, po.position FROM pages p
                JOIN page_order po ON po.page_id = p.id ORDER BY po.position"""
            )
        }

    assert reordered["second.png"] == (original["second.png"], 1)
    assert reordered["first.png"] == (original["first.png"], 2)


def test_runs_keep_analyses_and_prompt_versions_immutable(tmp_path: Path):
    output = tmp_path / "output"
    page_path = tmp_path / "page.png"
    _setup(output, tmp_path)
    storage.sync_pages(output, "default", [page_path])
    prompts = {"page_analysis": "Transcribe faithfully", "label": "Use a concise label"}

    first_run = storage.start_run(output, "default", "model", "key", prompts)
    storage.save_page_analysis(
        output,
        "default",
        first_run,
        {"page": 1, "file": "page.png", "title": "First result"},
        "model",
        "key",
    )
    section = {
        "section": 1,
        "start_page": 1,
        "end_page": 1,
        "label": "Document",
        "document_type": "report",
        "summary": "",
        "date": "",
        "places": [],
        "subjects": [],
        "label_confidence": 0.9,
        "lowest_boundary_confidence": 1.0,
    }
    storage.save_results(output, "default", [section], {}, first_run)

    second_run = storage.start_run(output, "default", "model", "key", prompts)
    storage.save_page_analysis(
        output,
        "default",
        second_run,
        {"page": 1, "file": "page.png", "title": "Second result"},
        "model",
        "key",
    )
    storage.save_results(output, "default", [section], {}, second_run)

    current = storage.load_collection_data(output, "default")
    with storage.connect(output) as database:
        analysis_count = database.execute("SELECT COUNT(*) FROM page_analyses").fetchone()[0]
        prompt_count = database.execute("SELECT COUNT(*) FROM prompt_versions").fetchone()[0]
        run_prompt_count = database.execute("SELECT COUNT(*) FROM run_prompts").fetchone()[0]

    assert current["run_id"] == second_run
    assert current["pages"][0]["title"] == "Second result"
    assert analysis_count == 2
    assert prompt_count == 2
    assert run_prompt_count == 4


def test_boundary_override_carries_to_next_run_but_not_after_undo(tmp_path: Path):
    output = tmp_path / "output"
    paths = [tmp_path / "page-1.png", tmp_path / "page-2.png"]
    _setup(output, tmp_path)
    storage.sync_pages(output, "default", paths)
    section = {
        "section": 1, "start_page": 1, "end_page": 2, "label": "Document",
        "document_type": "report", "summary": "", "date": "", "places": [],
        "subjects": [], "label_confidence": 0.9, "lowest_boundary_confidence": 0.8,
    }
    boundary = {2: {
        "right_page": 2, "starts_new_document": False, "confidence": 0.8,
        "reason": "Continues",
    }}

    first_run = storage.start_run(output, "default", "model", "key", {})
    analysis_ids = []
    for number, path in enumerate(paths, start=1):
        analysis_ids.append(storage.save_page_analysis(
            output, "default", first_run,
            {"page": number, "file": path.name}, "model", "key",
        ))
    storage.save_results(output, "default", [section], boundary, first_run)
    storage.set_boundary_override(output, "default", 2, True)

    second_run = storage.start_run(output, "default", "model", "key", {})
    for position, analysis_id in enumerate(analysis_ids, start=1):
        storage.attach_cached_page(output, second_run, position, analysis_id)
    storage.save_results(output, "default", [section], boundary, second_run)
    current = storage.load_collection_data(output, "default")
    assert current["boundaries"][0]["starts_new_document"] is True
    assert current["boundaries"][0]["review_source"] == "human"

    assert storage.undo_boundary_override(output, "default") is True
    third_run = storage.start_run(output, "default", "model", "key", {})
    for position, analysis_id in enumerate(analysis_ids, start=1):
        storage.attach_cached_page(output, third_run, position, analysis_id)
    storage.save_results(output, "default", [section], boundary, third_run)
    current = storage.load_collection_data(output, "default")
    assert current["boundaries"][0]["starts_new_document"] is False
    assert "review_source" not in current["boundaries"][0]


def test_old_schema_is_rejected_without_attempting_a_migration(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    with sqlite3.connect(output / storage.DATABASE_NAME) as database:
        database.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        database.execute("INSERT INTO metadata VALUES ('schema_version', '1')")

    with pytest.raises(ValueError, match="Use a fresh output directory"):
        storage.initialize(output)

    with sqlite3.connect(output / storage.DATABASE_NAME) as database:
        tables = {
            row[0]
            for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert tables == {"metadata"}
