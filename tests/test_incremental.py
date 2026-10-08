import pytest

from archival_organizer import cli, storage
from archival_organizer.review import effective_sections
from test_boundary_cache import BoundaryAI, archive, run
from test_migrations import version_four_archive


class RecordingAI(BoundaryAI):
    def __init__(self):
        super().__init__()
        self.label_calls = []
        self.fail_label_batch = None

    def label_groups(self, groups, offset):
        self.label_calls.append([[p['file'] for p in group] for group in groups])
        if len(self.label_calls) == self.fail_label_batch:
            raise RuntimeError('Label request failed')
        return super().label_groups(groups, offset)


def test_finished_collection_is_untouched_by_new_model_and_prompt(archive, monkeypatch):
    run(archive, RecordingAI())
    output = archive[1]
    storage.set_boundary_override(output, 'default', 2, True)
    storage.move_page_after(output, 'default', 2, 4)
    before = storage.load_collection_data(output, 'default')
    export = output / 'sections.csv'
    export_before = (export.read_bytes(), export.stat().st_mtime_ns)
    with storage.connect(output) as db:
        database_before = list(db.iterdump())
    monkeypatch.setattr(cli, 'PAGE_ANALYSIS_PROMPT', 'Changed page prompt')
    model = RecordingAI()
    run(archive, model, '--model', 'new-model')
    assert not model.page_calls and not model.boundary_calls and not model.label_calls
    assert storage.load_collection_data(output, 'default') == before
    assert (export.read_bytes(), export.stat().st_mtime_ns) == export_before
    with storage.connect(output) as db:
        assert list(db.iterdump()) == database_before


def test_all_finished_requires_no_api_key_or_client(archive, monkeypatch):
    source, output = archive
    run(archive, RecordingAI())
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(cli, 'ArchiveAI', lambda _: pytest.fail('No API client for skipped work'))
    cli._run_analysis(cli.parse_args(['analyze', str(source), '--output', str(output), '--model', 'new']))
    with storage.connect(output) as db:
        assert db.execute('SELECT COUNT(*) FROM analysis_runs').fetchone()[0] == 1


@pytest.mark.parametrize('explicit_model', [False, True])
@pytest.mark.parametrize('keep_limit', [False, True])
def test_default_model_change_reuses_completed_sample(archive, monkeypatch, explicit_model, keep_limit):
    source, output = archive
    run(archive, RecordingAI(), '--model', 'gpt-5.6-luna', '--limit', '2')
    before = storage.load_collection_data(output, 'default')
    monkeypatch.delenv('ARCHIVAL_MODEL', raising=False)
    monkeypatch.setattr(cli, 'DEFAULT_MODEL', 'gpt-6-luna')
    options = ['--model', 'gpt-6-luna'] if explicit_model else []
    if keep_limit:
        options += ['--limit', '2']
    args = cli.parse_args(['analyze', str(source), '--output', str(output), '--workers', '1', *options])
    assert args.model == 'gpt-6-luna'
    retry = RecordingAI()
    cli._process_collection(source, output, 'default', args, retry)
    if keep_limit:
        assert retry.page_calls == []
        assert retry.boundary_calls == []
        assert retry.label_calls == []
        assert storage.load_collection_data(output, 'default') == before
    else:
        assert retry.page_calls == [3, 4, 5]
        after = storage.load_collection_data(output, 'default')
        assert after['pages'][:2] == before['pages']


def test_legacy_completed_archive_is_adopted_without_analysis(version_four_archive, tmp_path):
    output = version_four_archive
    for number in (1, 2):
        (tmp_path / f'{number}.png').write_bytes(b'unchanged image')
    before = storage.load_collection_data(output, 'default')
    storage.initialize(output)
    model = RecordingAI()
    args = cli.parse_args(['analyze', str(tmp_path), '--model', 'new-model'])
    cli._process_collection(tmp_path, output, 'default', args, model)
    assert not model.page_calls and not model.boundary_calls and not model.label_calls
    assert storage.load_collection_data(output, 'default') == before
    with storage.connect(output) as db:
        assert db.execute('SELECT COUNT(*) FROM analysis_runs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM analysis_sources').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM run_sources').fetchone()[0] == 1
    # Subsequent edits are detected using the adopted fingerprints.
    (tmp_path / '2.png').write_bytes(b'updated image')
    cli._process_collection(tmp_path, output, 'default', args, model)
    assert model.page_calls == [2]


def test_changed_scan_updates_only_affected_work_across_model_change(archive):
    model = RecordingAI()
    model.split = True
    run(archive, model)
    (archive[0] / '2.png').write_bytes(b'changed scan')
    retry = RecordingAI()
    retry.split = True
    retry.page_changes[2] = {'title': 'Changed title'}
    run(archive, retry, '--model', 'new-model')
    assert retry.page_calls == [2]
    assert retry.boundary_calls == [[1, 2, 3]]
    assert retry.label_calls == [[['2.png']]]
    data = storage.load_collection_data(archive[1], 'default')
    assert [p['model'] for p in data['pages']] == ['test-model', 'new-model', 'test-model', 'test-model', 'test-model']
    with storage.connect(archive[1]) as db:
        models = {r[0] for r in db.execute('SELECT model FROM stage_results WHERE run_id = ?', (data['run_id'],))}
        assert models == {'test-model', 'new-model'}


def test_added_scan_and_removed_scan_keep_remaining_extractions(archive):
    run(archive, RecordingAI())
    (archive[0] / '6.png').write_bytes(b'new page')
    retry = RecordingAI()
    run(archive, retry)
    assert retry.page_calls == [6]
    assert retry.boundary_calls == [[5, 6]]
    (archive[0] / '2.png').unlink()
    removed = RecordingAI()
    run(archive, removed)
    assert removed.page_calls == []
    data = storage.load_collection_data(archive[1], 'default')
    assert [p['file'] for p in data['pages']] == ['1.png', '3.png', '4.png', '5.png', '6.png']
    assert [p['page'] for p in data['pages']] == list(range(1, 6))


def test_label_only_refresh_keeps_pages_and_boundaries(archive, monkeypatch):
    run(archive, RecordingAI())
    before = storage.load_collection_data(archive[1], 'default')
    monkeypatch.setattr(cli, 'PAGE_ANALYSIS_PROMPT', 'New extraction prompt')
    retry = RecordingAI()
    run(archive, retry, '--refresh-labels', '--model', 'new-model')
    assert retry.page_calls == [] and retry.boundary_calls == [] and retry.review_calls == []
    assert len(retry.label_calls) == 1
    after = storage.load_collection_data(archive[1], 'default')
    assert after['pages'] == before['pages']
    assert [b['reason'] for b in after['boundaries']] == [b['reason'] for b in before['boundaries']]


def test_full_reanalysis_is_explicit(archive):
    run(archive, RecordingAI())
    retry = RecordingAI()
    run(archive, retry, '--reanalyze', '--model', 'new-model')
    assert retry.page_calls == list(range(1, 6))
    assert retry.boundary_calls == [[1, 2, 3], [3, 4, 5]]
    assert len(retry.label_calls) == 1
    assert all(p['model'] == 'new-model' for p in storage.load_collection_data(archive[1], 'default')['pages'])


def test_interrupted_label_batches_are_saved_and_resumed(archive):
    first = RecordingAI()
    first.split = True
    first.fail_label_batch = 2
    with pytest.raises(RuntimeError, match='Label request failed'):
        run(archive, first, '--label-batch', '2')
    retry = RecordingAI()
    retry.split = True
    run(archive, retry, '--label-batch', '2', '--model', 'new-model')
    assert retry.page_calls == [] and retry.boundary_calls == []
    assert retry.label_calls == [[['3.png'], ['4.png']], [['5.png']]]


@pytest.mark.parametrize('option', ['--reanalyze', '--refresh-boundaries', '--refresh-labels'])
def test_failed_explicit_update_preserves_completed_result_and_resumes(archive, option):
    initial = RecordingAI()
    initial.split = True
    run(archive, initial)
    before = storage.load_collection_data(archive[1], 'default')
    export = (archive[1] / 'sections.csv').read_bytes()
    failing = RecordingAI()
    failing.split = True
    if option == '--refresh-boundaries':
        failing.fail_batch = 3
    else:
        failing.fail_label_batch = 2
    with pytest.raises(RuntimeError):
        run(archive, failing, option, '--label-batch', '2')
    assert storage.load_collection_data(archive[1], 'default') == before
    assert (archive[1] / 'sections.csv').read_bytes() == export
    retry = RecordingAI()
    retry.split = True
    run(archive, retry, '--label-batch', '2')
    assert retry.page_calls == []
    if option == '--refresh-boundaries':
        assert retry.boundary_calls == [[3, 4, 5]]
    else:
        assert retry.boundary_calls == []
        assert retry.label_calls == [[['3.png'], ['4.png']], [['5.png']]]
    assert storage.load_collection_data(archive[1], 'default')['run_id'] != before['run_id']


def test_source_change_during_analysis_does_not_finalize(archive):
    class ChangingAI(RecordingAI):
        def label_groups(self, groups, offset):
            (archive[0] / '6.png').touch()
            return super().label_groups(groups, offset)
    with pytest.raises(ValueError, match='changed during analysis'):
        run(archive, ChangingAI())
    with storage.connect(archive[1]) as db:
        assert db.execute('SELECT COUNT(*) FROM analysis_runs WHERE completed_at IS NOT NULL').fetchone()[0] == 0


def test_manual_order_survives_addition_and_stale_label_is_flagged(archive):
    run(archive, RecordingAI())
    output = archive[1]
    storage.move_page_after(output, 'default', 2, 4)
    before = storage.load_collection_data(output, 'default')
    ids = [p['page_id'] for p in before['pages']]
    label = {'label': 'Reviewed title', 'document_type': 'report', 'summary': '', 'date': '',
             'places': [], 'subjects': [], 'confidence': 0.9}
    storage.save_reviewed_label(output, 'default', ids, label)
    retry = RecordingAI()
    retry.page_changes[2] = {'title': 'New text'}
    run(archive, retry, '--refresh-pages', '2')
    updated = storage.load_collection_data(output, 'default')
    assert [p['page_id'] for p in updated['pages']] == ids
    assert updated['reviewed_labels'][tuple(ids)]['stale']
    pages = {p['page']: p for p in updated['pages']}
    # A human-reviewed label remains authoritative, but needs another check.
    sections = effective_sections(updated['sections'], pages,
                                  {n: {'starts_new_document': False} for n in range(2, 6)},
                                  updated['reviewed_labels'])
    assert sections[0]['label'] == 'Reviewed title'
    assert sections[0]['label_needs_review']
    (archive[0] / '6.png').touch()
    run(archive, RecordingAI())
    added = storage.load_collection_data(output, 'default')
    assert [p['page_id'] for p in added['pages'][:5]] == ids
    assert any('manual order' in warning for warning in added['review_warnings'])


def test_legacy_archive_with_added_scan_reuses_existing_extractions(version_four_archive, tmp_path):
    output = version_four_archive
    for number in (1, 2, 3):
        (tmp_path / f'{number}.png').touch()
    storage.initialize(output)
    model = RecordingAI()
    cli._process_collection(tmp_path, output, 'default', cli.parse_args(['analyze', str(tmp_path)]), model)
    assert model.page_calls == [3]
    with storage.connect(output) as db:
        assert db.execute('SELECT COUNT(*) FROM page_analyses').fetchone()[0] == 3


def test_labels_survive_page_number_changes(archive):
    initial = RecordingAI()
    initial.split = True
    run(archive, initial)
    (archive[0] / '0.png').touch()
    retry = RecordingAI()
    retry.split = True
    run(archive, retry)
    assert retry.page_calls == [0]
    assert retry.label_calls == [[['0.png']]]
    data = storage.load_collection_data(archive[1], 'default')
    assert len(data['sections']) == 6
    assert data['pages'][1]['file'] == '1.png'
    assert data['pages'][1]['page'] == 2
