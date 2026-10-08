from argparse import Namespace
import hashlib
import sys

import pytest

from archival_organizer import cli


def test_cli_uses_analyze_and_relabel_subcommands():
    analyze = cli.parse_args(["analyze", "/scans"])
    relabel = cli.parse_args([
        "relabel", "/archive", "--collection", "box/folder", "--dry-run"
    ])

    assert analyze.command == "analyze"
    assert analyze.workers == 4
    assert analyze.collection == []
    assert relabel.command == "relabel"
    assert relabel.collection == ["box/folder"]
    assert relabel.dry_run is True


@pytest.fixture
def collection_archive(tmp_path, monkeypatch):
    source, output = tmp_path / "scans", tmp_path / "archive"
    page_calls = []
    for folder in ("dir1", "dir2", "dir1/child"):
        directory = source / folder
        directory.mkdir(parents=True, exist_ok=True)
        for number in (1, 2):
            (directory / f"{number}.png").touch()

    class FakeAI:
        def __init__(self, model):
            pass

        def analyze_page(self, path):
            page_calls.append(path.relative_to(source).as_posix())
            return {"document_type": "report", "title": str(path), "transcription": "Text."}

        def decide_boundaries(self, pages):
            return [{"right_page": page["page"], "starts_new_document": False,
                     "confidence": 0.9, "reason": "Continuation"} for page in pages[1:]]

        def label_groups(self, groups, offset):
            return [{"group": number, "label": "Report", "document_type": "report",
                     "summary": "", "date": "", "places": [], "subjects": [],
                     "confidence": 0.9} for number, _ in enumerate(groups, start=offset)]

    monkeypatch.setattr(cli, "ArchiveAI", FakeAI)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def run(*options):
        cli._run_analysis(cli.parse_args([
            "analyze", str(source), "--output", str(output), "--workers", "1", *options,
        ]))

    return source, output, page_calls, run


def test_selected_collections_share_archive_and_preserve_reviews(collection_archive):
    source, output, calls, run = collection_archive
    first_id = hashlib.sha256(b"dir1").hexdigest()[:12]
    second_id = hashlib.sha256(b"dir2").hexdigest()[:12]
    run("--collection", "dir1")
    assert calls == ["dir1/1.png", "dir1/2.png", "dir1/child/1.png", "dir1/child/2.png"]
    assert {row["relative_path"] for row in cli.storage.list_collections(output)} == {"dir1", "dir1/child"}
    cli.storage.set_boundary_override(output, first_id, 2, True)
    before = cli.storage.load_collection_data(output, first_id)

    calls.clear()
    run("--collection", "dir2")
    assert calls == ["dir2/1.png", "dir2/2.png"]
    assert cli.storage.load_collection_data(output, first_id) == before
    second = cli.storage.load_collection_data(output, second_id)
    assert second["collection"]["input_directory"] == str(source / "dir2")

    calls.clear()
    run()
    assert calls == []
    after = cli.storage.load_collection_data(output, first_id)
    assert [p["page_id"] for p in after["pages"]] == [p["page_id"] for p in before["pages"]]
    assert after["boundaries"][0]["review_source"] == "human"
    with cli.storage.connect(output) as database:
        assert database.execute("SELECT COUNT(*) FROM page_analyses").fetchone()[0] == 6
        assert database.execute("SELECT COUNT(*) FROM collections WHERE id = 'default'").fetchone()[0] == 0


def test_collection_selection_deduplicates_and_keeps_discovery_order(collection_archive):
    _source, output, calls, run = collection_archive
    run("--collection", "dir2", "--collection", "./dir1/", "--collection", "dir1/child")
    assert calls == ["dir1/1.png", "dir1/2.png", "dir1/child/1.png", "dir1/child/2.png",
                     "dir2/1.png", "dir2/2.png"]
    with cli.storage.connect(output) as database:
        assert database.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 3


def test_parent_without_images_selects_descendants_with_per_collection_limit(collection_archive):
    source, output, calls, run = collection_archive
    for path in (source / "dir1").glob("*.png"):
        path.unlink()
    for folder in ("dir1/child2", "dir10"):
        (source / folder).mkdir()
        for number in (1, 2):
            (source / folder / f"{number}.png").touch()
    run("--collection", "dir1", "--limit", "1")
    assert calls == ["dir1/child/1.png", "dir1/child2/1.png"]
    assert {row["relative_path"] for row in cli.storage.list_collections(output)} == {
        "dir1/child", "dir1/child2",
    }
    calls.clear()
    run("--collection", "dir1")
    assert calls == ["dir1/child/2.png", "dir1/child2/2.png"]


@pytest.mark.parametrize("selector", ["missing", "dir", "..", "../dir1", "/dir1", ""])
def test_invalid_collection_fails_before_side_effects(collection_archive, monkeypatch, selector):
    _source, output, calls, run = collection_archive
    monkeypatch.setattr(cli, "ArchiveAI", lambda _: pytest.fail("AI must not be initialized"))
    with pytest.raises(SystemExit, match="Collection"):
        run("--collection", "dir1", "--collection", selector)
    assert not output.exists()
    assert calls == []


def test_refresh_pages_in_selected_collection(collection_archive):
    _source, output, calls, run = collection_archive
    run("--collection", "dir2")
    calls.clear()
    run("--collection", "dir2", "--refresh-pages", "2")
    assert calls == ["dir2/2.png"]
    with pytest.raises(SystemExit, match="requires exactly one collection"):
        run("--collection", "dir1", "--refresh-pages", "1")
    with pytest.raises(SystemExit, match="requires exactly one collection"):
        run("--collection", "dir1", "--collection", "dir2", "--refresh-pages", "1")
    with pytest.raises(SystemExit, match="requires exactly one collection"):
        run("--refresh-pages", "1")


@pytest.mark.parametrize("nested", [False, True])
def test_selecting_input_itself_preserves_unfiltered_identity(collection_archive, nested):
    source, output, calls, _run = collection_archive
    root = source if nested else source / "dir2"
    (root / "root.png").touch()
    cli._run_analysis(cli.parse_args([
        "analyze", str(root), "--output", str(output), "--collection", ".", "--workers", "1",
    ]))
    rows = cli.storage.list_collections(output)
    assert len(rows) == (4 if nested else 1)
    root_row = next(row for row in rows if row["relative_path"] == ".")
    assert root_row["id"] == (hashlib.sha256(b".").hexdigest()[:12] if nested else "default")
    assert calls == (["root.png", "dir1/1.png", "dir1/2.png", "dir1/child/1.png",
                      "dir1/child/2.png", "dir2/1.png", "dir2/2.png"] if nested else
                     ["dir2/1.png", "dir2/2.png", "dir2/root.png"])


def test_relabel_dry_run_lists_targets_without_api_key(tmp_path, monkeypatch, capsys):
    output = tmp_path / "archive"
    output.mkdir()
    (output / "archive.sqlite3").touch()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli.storage, "initialize", lambda _output: None)
    monkeypatch.setattr(cli.storage, "list_collections", lambda _output: [{
        "id": "abc123", "relative_path": "box/folder"
    }])
    monkeypatch.setattr(cli, "label_targets", lambda *_args, **_kwargs: [{
        "section": 3, "start_page": 8, "end_page": 12
    }])

    cli._run_relabel(Namespace(
        output=output,
        collection=[],
        all_reviewed=False,
        dry_run=True,
        model=None,
        label_batch=16,
    ))

    output_text = capsys.readouterr().out
    assert "box/folder" in output_text
    assert "section 3: pages 8-12" in output_text
    assert "Dry run: 1 section(s)" in output_text


def test_analyze_failure_exits_without_finalizing_collection(tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.mkdir()
    (source / "page-1.png").touch()
    (source / "page-2.png").touch()
    output = tmp_path / "output"

    class FailingAI:
        def __init__(self, model):
            pass

        def analyze_page(self, path):
            if path.name == "page-1.png":
                raise OSError("corrupt image")
            # Empty text and low confidence still count as a successful analysis.
            return {"document_type": "blank", "title": "", "transcription": "", "extraction_confidence": 0}

        def decide_boundaries(self, pages):
            pytest.fail("Boundary detection must not run after a page failure")

        def label_groups(self, groups, offset):
            pytest.fail("Labeling must not run after a page failure")

    monkeypatch.setattr(cli, "ArchiveAI", FailingAI)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", [
        "archival-organizer", "analyze", str(source), "--output", str(output),
    ])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert "1 failed, 1 successful/cached, 0 not attempted" in str(error.value)
    assert "page-1.png: OSError: corrupt image" in str(error.value)
    assert not (output / "sections.csv").exists()
    with cli.storage.connect(output) as database:
        assert database.execute("SELECT COUNT(*) FROM page_analyses").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert database.execute(
            "SELECT COUNT(*) FROM analysis_runs WHERE completed_at IS NOT NULL"
        ).fetchone()[0] == 0
