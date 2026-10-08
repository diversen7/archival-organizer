from copy import deepcopy
import json

import pytest

from archival_organizer import ai, cli, prompts, storage


class BoundaryAI(ai.ArchiveAI):
    """Exercise real boundary request construction without API calls."""

    def __init__(self):
        self.page_calls = []
        self.boundary_calls = []
        self.review_calls = []
        self.page_changes = {}
        self.fail_batch = None
        self.fail_labels = False
        self.split = False
        self.reason = "Initial decision"

    def analyze_page(self, path):
        number = int(path.stem)
        self.page_calls.append(number)
        return {
            "document_type": "report", "title": f"Page {number}", "summary": "",
            "date": "", "places": [], "subjects": [], "transcription": "Original text",
            **self.page_changes.get(number, {}),
        }

    def _structured(self, prompt, schema, image_url=None):
        assert schema is ai.BOUNDARY_SCHEMA
        if "\nPage evidence:\n" in prompt:
            pages = json.loads(prompt.split("\nPage evidence:\n", 1)[1])
            self.boundary_calls.append([page["page"] for page in pages])
            if pages[0]["page"] == self.fail_batch:
                raise RuntimeError("Boundary request failed")
            right_pages = [page["page"] for page in pages[1:]]
        else:
            cases = json.loads(prompt.split("\nCases:\n", 1)[1])
            self.review_calls.append(cases)
            right_pages = [case["right_page"] for case in cases]
        return {"boundaries": [{
            "right_page": number, "starts_new_document": self.split,
            "confidence": 0.9, "reason": self.reason,
        } for number in right_pages]}

    def label_groups(self, groups, offset):
        if self.fail_labels:
            raise RuntimeError("Label request failed")
        return [{
            "group": number, "label": "Document", "document_type": "report", "summary": "",
            "date": "", "places": [], "subjects": [], "confidence": 0.9,
        } for number, group in enumerate(groups, start=offset)]


@pytest.fixture
def archive(tmp_path):
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir()
    for number in range(1, 6):
        (source / f"{number}.png").touch()
    storage.initialize(output)
    storage.upsert_collection(output, "default", "Test", ".", source)
    return source, output


def run(archive, model, *options):
    source, output = archive
    args = cli.parse_args([
        "analyze", str(source), "--output", str(output), "--model", "test-model",
        "--boundary-batch", "2", "--workers", "1", *options,
    ])
    return cli._process_collection(source, output, "default", args, model)


def decisions(data):
    # Completed runs keep their own boundary row identities.
    return [{k: v for k, v in item.items() if k != "boundary_id"}
            for item in data["original_boundaries"]]


def test_rerun_reuses_batches_and_preserves_human_overrides(archive, capsys):
    model = BoundaryAI()
    run(archive, model)
    output = archive[1]
    before = storage.load_collection_data(output, "default")
    storage.set_boundary_override(output, "default", 2, True)

    # A new client and run must still find the persisted cache.
    retry = BoundaryAI()
    run(archive, retry)
    after = storage.load_collection_data(output, "default")
    assert retry.page_calls == []
    assert retry.boundary_calls == []
    assert before["run_id"] == after["run_id"]
    assert decisions(before) == decisions(after)
    assert after["boundaries"][0]["review_source"] == "human"
    assert "completed, sources unchanged" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["boundary", "labels"])
def test_interrupted_run_keeps_completed_batches(archive, failure):
    model = BoundaryAI()
    model.fail_batch = 3 if failure == "boundary" else None
    model.fail_labels = failure == "labels"
    with pytest.raises(RuntimeError, match="request failed"):
        run(archive, model)
    with storage.connect(archive[1]) as database:
        assert database.execute("SELECT COUNT(*) FROM boundaries").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM boundary_cache").fetchone()[0] == (
            1 if failure == "boundary" else 2
        )

    retry = BoundaryAI()
    run(archive, retry)
    assert retry.page_calls == []
    assert retry.boundary_calls == ([[3, 4, 5]] if failure == "boundary" else [])


def test_changed_page_invalidates_only_batches_containing_it(archive):
    model = BoundaryAI()
    run(archive, model)
    retry = BoundaryAI()
    retry.page_changes[2] = {"title": "Corrected title"}
    run(archive, retry, "--refresh-pages", "2")
    assert retry.boundary_calls == [[1, 2, 3]]

    # Page 3 is shared context and affects both windows.
    retry.boundary_calls.clear()
    retry.page_changes[3] = {"title": "Corrected shared context"}
    run(archive, retry, "--refresh-pages", "3")
    assert retry.boundary_calls == [[1, 2, 3], [3, 4, 5]]


@pytest.mark.parametrize("change", ["model", "prompt", "schema", "batch", "order"])
def test_configuration_changes_do_not_reopen_completed_collections(archive, monkeypatch, change):
    run(archive, BoundaryAI())
    options = []
    if change == "model":
        options = ["--model", "different-model"]
    elif change == "prompt":
        monkeypatch.setattr(prompts, "BOUNDARY_PROMPT", prompts.BOUNDARY_PROMPT + " New guidance.")
    elif change == "schema":
        schema = deepcopy(ai.BOUNDARY_SCHEMA["schema"])
        schema["description"] = "New schema version"
        monkeypatch.setitem(ai.BOUNDARY_SCHEMA, "schema", schema)
    elif change == "batch":
        options = ["--boundary-batch", "3"]
    else:
        monkeypatch.setattr(cli, "discover_pages", lambda path: list(reversed(sorted(path.glob("*.png")))))
    retry = BoundaryAI()
    run(archive, retry, *options)
    assert len(retry.boundary_calls) == (2 if change == "order" else 0)


def test_refresh_replaces_cache_without_reanalyzing_pages(archive):
    run(archive, BoundaryAI())
    retry = BoundaryAI()
    retry.reason = "Refreshed decision"
    run(archive, retry, "--refresh-boundaries")
    assert retry.page_calls == []
    assert len(retry.boundary_calls) == 2
    cached = BoundaryAI()
    run(archive, cached)
    assert cached.boundary_calls == []
    data = storage.load_collection_data(archive[1], "default")
    assert all(item["reason"] == "Refreshed decision" for item in data["original_boundaries"])


def test_review_cache_tracks_transcription_and_prompt_and_does_not_accumulate_flags(archive, monkeypatch):
    model = BoundaryAI()
    model.split = True
    model.page_changes = {1: {"ends_mid_sentence": True}, 2: {"starts_mid_sentence": True}}
    run(archive, model)
    assert len(model.review_calls) == 1
    before = decisions(storage.load_collection_data(archive[1], "default"))
    run(archive, model)
    assert len(model.review_calls) == 1
    assert decisions(storage.load_collection_data(archive[1], "default")) == before

    # Initial batches omit transcription, but the review includes excerpts.
    model.page_changes[2]["transcription"] = "Corrected continuation text"
    run(archive, model, "--refresh-pages", "2")
    assert len(model.boundary_calls) == 2
    assert len(model.review_calls) == 2

    monkeypatch.setattr(prompts, "BOUNDARY_REVIEW_PROMPT", prompts.BOUNDARY_REVIEW_PROMPT + " New guidance.")
    run(archive, model, "--refresh-pages", "1")
    assert len(model.boundary_calls) == 2
    assert len(model.review_calls) == 3
    run(archive, model, "--refresh-boundaries")
    assert len(model.boundary_calls) == 4
    assert len(model.review_calls) == 4


def test_review_survives_labeling_failure(archive):
    model = BoundaryAI()
    model.split = True
    model.page_changes = {1: {"ends_mid_sentence": True}, 2: {"starts_mid_sentence": True}}
    model.fail_labels = True
    with pytest.raises(RuntimeError, match="Label request failed"):
        run(archive, model)
    model.fail_labels = False
    run(archive, model)
    assert len(model.boundary_calls) == 2
    assert len(model.review_calls) == 1


def test_single_page_has_no_boundary_requests(archive):
    model = BoundaryAI()
    run(archive, model, "--limit", "1")
    assert model.boundary_calls == []
    assert model.review_calls == []
