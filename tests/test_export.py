import json
import shutil
import sqlite3
import sys

import pytest

from archival_organizer import cli, export, storage
from archival_organizer.migrations import BASE_SCHEMA
from test_cli_commands import collection_archive  # noqa: F401


def read_rows(destination, name):
    return [json.loads(line) for line in (destination / f"{name}.jsonl").read_text().splitlines()]


def database_rows(output):
    with storage.connect(output) as database:
        return {row[0]: [tuple(item) for item in database.execute(f'SELECT * FROM "{row[0]}"')]
                for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_complete_export_preserves_history_and_reviews(collection_archive, tmp_path, monkeypatch, capsys):
    source, output, _, run = collection_archive
    run()
    collection = next(c for c in storage.list_collections(output) if c['relative_path'] == 'dir2')
    cid = collection['id']
    storage.set_boundary_override(output, cid, 2, True, reviewer_name="Åse")
    data = storage.load_collection_data(output, cid)
    storage.set_document_flag(output, cid, [data['pages'][1]['page_id']], True, "Åse")
    # Include an unfinished run and a collection with no completed analysis.
    storage.start_run(output, cid, 'unfinished-model', 'key', {})
    storage.upsert_collection(output, 'empty', 'Empty', 'empty', source / 'empty')
    before = database_rows(output)
    shutil.rmtree(source)
    monkeypatch.delenv('OPENAI_API_KEY')
    destination = tmp_path / 'export'
    monkeypatch.setattr(sys, 'argv', ['archival-organizer', 'export', str(output),
                                     '--destination', str(destination)])
    cli.main()
    assert database_rows(output) == before
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['export_version'] == 1
    assert manifest['images_included'] is False
    progress = capsys.readouterr().out
    assert '[export] Database snapshot: 100%' in progress
    assert '[export] History table' in progress
    assert '[export] Reviewed collection 4/4:' in progress
    assert f"Complete: {sum(manifest['record_counts'].values()):,} JSONL records" in progress
    assert 'MiB in' in progress
    for table, rows in before.items():
        exported = read_rows(destination, f'history/{table}')
        assert len(exported) == len(rows) == manifest['record_counts'][f'history/{table}.jsonl']
        if table != 'collections':
            assert sorted(map(repr, [tuple(row.values()) for row in exported])) == sorted(map(repr, rows))
    collections = read_rows(destination, 'collections')
    assert next(c for c in collections if c['id'] == 'empty')['latest_completed_run_id'] is None
    assert all(c['input_directory'] == c['relative_path'] for c in collections)
    pages = read_rows(destination, 'pages')
    assert {p['image_path'] for p in pages} == {
        f'{folder}/{n}.png' for folder in ('dir1', 'dir1/child', 'dir2') for n in (1, 2)
    }
    documents = read_rows(destination, 'documents')
    reviewed = [d for d in documents if d['collection_id'] == cid]
    assert len(reviewed) == 2
    assert reviewed[1]['flagged'] is True
    assert all(d['review_adjusted'] and d['label_needs_review'] for d in reviewed)
    assert all(p['document_id'] in {d['id'] for d in documents} for p in pages)
    assert next(p for p in pages if p['collection_id'] == cid and p['position'] == 2)[
        'boundary_before']['review_source'] == 'human'
    assert 'Åse' in (destination / 'history/edit_history.jsonl').read_text()
    assert all(p.suffix in {'.json', '.jsonl'} for p in destination.rglob('*') if p.is_file())


def test_failure_cleanup_and_existing_destination(collection_archive, tmp_path, monkeypatch):
    _, output, _, run = collection_archive
    run('--collection', 'dir2')
    destination = tmp_path / 'export'
    before = database_rows(output)

    def fail(*args):
        raise ValueError('injected failure')

    monkeypatch.setattr(export, '_current', fail)
    with pytest.raises(ValueError, match='injected failure'):
        export.export_archive(output, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob('.archive-export-*'))
    assert database_rows(output) == before
    destination.mkdir()
    (destination / 'keep').write_text('keep')
    with pytest.raises(FileExistsError):
        export.export_archive(output, destination)
    assert (destination / 'keep').read_text() == 'keep'


def test_version_four_export_only_upgrades_snapshot(tmp_path):
    output = tmp_path / 'archive'
    output.mkdir()
    with sqlite3.connect(output / storage.DATABASE_NAME) as database:
        for statement in BASE_SCHEMA:
            database.execute(statement)
        database.execute("INSERT INTO metadata VALUES ('schema_version', '4')")
    before = (output / storage.DATABASE_NAME).read_bytes()
    destination = export.export_archive(output, tmp_path / 'export')
    assert (output / storage.DATABASE_NAME).read_bytes() == before
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['source_schema_version'] == 4
    assert 'edit_history' not in manifest['history_schema']
    assert read_rows(destination, 'collections') == []


@pytest.mark.parametrize('version', [3, 999])
def test_unsupported_versions_leave_source_unchanged(tmp_path, version):
    storage.initialize(tmp_path / 'archive')
    with storage.connect(tmp_path / 'archive') as database:
        database.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(version),))
    before = database_rows(tmp_path / 'archive')
    with pytest.raises(ValueError, match='Unsupported database schema'):
        export.export_archive(tmp_path / 'archive', tmp_path / 'export')
    assert database_rows(tmp_path / 'archive') == before
    assert not (tmp_path / 'export').exists()


def test_missing_database_is_not_created(tmp_path):
    with pytest.raises(ValueError, match='not found'):
        export.export_archive(tmp_path / 'missing', tmp_path / 'export')
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('folder', ['.', 'nested/æø å'])
def test_relative_paths(folder):
    expected = 'scan 1.png' if folder == '.' else f'{folder}/scan 1.png'
    assert export._relative_path(folder, 'scan 1.png') == expected


@pytest.mark.parametrize('path', ['/absolute', '../escape', 'C:/scans', 'box\\folder'])
def test_unsafe_relative_paths_rejected(path):
    with pytest.raises(ValueError, match='unsafe relative'):
        export._relative_path(path, 'scan.png')


def test_snapshot_is_consistent_during_concurrent_edit(collection_archive, tmp_path, monkeypatch):
    _, output, _, run = collection_archive
    run('--collection', 'dir2')
    cid = storage.list_collections(output)[0]['id']
    original_history = export._history

    def edit_after_backup(*args):
        storage.set_boundary_override(output, cid, 2, True, reviewer_name='Concurrent')
        return original_history(*args)

    monkeypatch.setattr(export, '_history', edit_after_backup)
    destination = export.export_archive(output, tmp_path / 'export')
    assert read_rows(destination, 'history/boundary_overrides') == []
    assert len(read_rows(destination, 'documents')) == 1
    assert storage.load_collection_data(output, cid)['overrides']


def test_reviewed_order_labels_and_previous_runs(collection_archive, tmp_path):
    _, output, _, run = collection_archive
    run('--collection', 'dir2')
    cid = storage.list_collections(output)[0]['id']
    run('--collection', 'dir2', '--reanalyze')
    storage.move_page_after(output, cid, 1, 2, 'Reviewer')
    data = storage.load_collection_data(output, cid)
    page_ids = [page['page_id'] for page in data['pages']]
    storage.save_reviewed_label(output, cid, page_ids, {
        'label': 'Ændret titel', 'document_type': 'letter', 'summary': 'Reviewed summary',
        'date': '1920', 'places': ['København'], 'subjects': ['History'], 'confidence': 0.95,
    }, 'Reviewer')
    destination = export.export_archive(output, tmp_path / 'export')
    pages = read_rows(destination, 'pages')
    documents = read_rows(destination, 'documents')
    assert [p['image_path'] for p in pages] == ['dir2/2.png', 'dir2/1.png']
    assert documents[0]['page_ids'] == page_ids
    assert documents[0]['label'] == 'Ændret titel'
    assert documents[0]['label_source'] == 'regenerated'
    assert documents[0]['label_needs_review'] is False
    assert len(read_rows(destination, 'history/analysis_runs')) == 2
    assert len(read_rows(destination, 'history/page_analyses')) == 4


def test_publish_failure_removes_reserved_destination(tmp_path, monkeypatch):
    from pathlib import Path

    output = tmp_path / 'archive'
    storage.initialize(output)

    def fail(*args):
        raise OSError('publish failed')

    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError, match='publish failed'):
        export.export_archive(output, tmp_path / 'export')
    assert not (tmp_path / 'export').exists()
    assert not list(tmp_path.glob('.archive-export-*'))
