import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from openai import AuthenticationError, PermissionDeniedError

from archival_organizer import storage
from test_boundary_cache import BoundaryAI, archive, run
from archival_organizer.cli import (
    PageAnalysisError, _analysis_key, _load_or_analyze_pages, _page_selection, parse_args,
)


class FakeAI:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.lock = threading.Lock()

    def analyze_page(self, path: Path) -> dict:
        with self.lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return {
            "document_type": "report",
            "title": path.stem,
            "transcription": f"Text from {path.name}",
        }


def _start_run(output: Path, paths: list[Path]) -> int:
    storage.sync_pages(output, "test", paths)
    return storage.start_run(output, "test", "test-model", _analysis_key(), {})


def test_default_page_worker_count_is_four(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["archival-organizer", "analyze", "/scans"])

    assert parse_args().workers == 4


def test_page_selection_supports_individual_pages_and_ranges():
    assert _page_selection("38,42-44") == frozenset({38, 42, 43, 44})


def test_uncached_pages_are_analyzed_concurrently_and_returned_in_order(tmp_path: Path):
    paths = [tmp_path / f"page-{number}.png" for number in range(1, 7)]
    ai = FakeAI()
    storage.initialize(tmp_path / "output")
    storage.upsert_collection(tmp_path / "output", "test", "Test", ".", tmp_path)
    run_id = _start_run(tmp_path / "output", paths)

    pages = _load_or_analyze_pages(
        paths, tmp_path / "output", "test", run_id, ai, "test-model", workers=4
    )

    assert ai.max_active == 4
    assert [page["page"] for page in pages] == [1, 2, 3, 4, 5, 6]
    assert [page["file"] for page in pages] == [path.name for path in paths]
    with storage.connect(tmp_path / "output") as database:
        assert database.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 6


def test_cached_pages_do_not_use_workers(tmp_path: Path):
    paths = [tmp_path / "page-1.png"]
    output = tmp_path / "output"
    storage.initialize(output)
    storage.upsert_collection(output, "test", "Test", ".", tmp_path)
    seed_run = _start_run(output, paths)
    storage.save_page_analysis(output, "test", seed_run, {
        "page": 1,
        "file": "page-1.png",
        "model": "test-model",
    }, "test-model", _analysis_key())
    run_id = storage.start_run(output, "test", "test-model", _analysis_key(), {})
    ai = FakeAI()

    pages = _load_or_analyze_pages(
        paths, output, "test", run_id, ai, "test-model", workers=4
    )

    assert pages[0]["file"] == "page-1.png"
    assert ai.calls == 0


def test_selected_cached_page_is_reanalyzed(tmp_path: Path):
    paths = [tmp_path / "page-1.png", tmp_path / "page-2.png"]
    output = tmp_path / "output"
    storage.initialize(output)
    storage.upsert_collection(output, "test", "Test", ".", tmp_path)
    seed_run = _start_run(output, paths)
    for number, path in enumerate(paths, start=1):
        storage.save_page_analysis(output, "test", seed_run, {
            "page": number,
            "file": path.name,
            "model": "test-model",
            "title": "old",
        }, "test-model", _analysis_key())
    run_id = storage.start_run(output, "test", "test-model", _analysis_key(), {})
    ai = FakeAI()

    pages = _load_or_analyze_pages(
        paths,
        output,
        "test",
        run_id,
        ai,
        "test-model",
        workers=4,
        refresh_pages=frozenset({2}),
    )

    assert ai.calls == 1
    assert pages[0]["title"] == "old"
    assert pages[1]["title"] == "page-2"


def test_page_failures_are_summarized_and_successes_reused_on_retry(tmp_path: Path):
    paths = [tmp_path / f"page-{number}.png" for number in range(1, 5)]
    output = tmp_path / "output"
    storage.initialize(output)
    storage.upsert_collection(output, "test", "Test", ".", tmp_path)
    run_id = _start_run(output, paths)

    class FailingAI(FakeAI):
        def analyze_page(self, path):
            if path == paths[0]:
                raise OSError("corrupt image")
            if path == paths[2]:
                raise ValueError("invalid response JSON")
            return super().analyze_page(path)

    ai = FailingAI()
    with pytest.raises(PageAnalysisError) as error:
        _load_or_analyze_pages(paths, output, "test", run_id, ai, "test-model", workers=2)

    summary = str(error.value)
    assert "2 failed, 2 successful/cached, 0 not attempted" in summary
    assert "page-1.png: OSError: corrupt image" in summary
    assert "page-3.png: ValueError: invalid response JSON" in summary
    assert summary.index("page-1.png") < summary.index("page-3.png")
    assert ai.calls == 2

    retry_ai = FakeAI()
    retry_run = _start_run(output, paths)
    pages = _load_or_analyze_pages(
        paths, output, "test", retry_run, retry_ai, "test-model", workers=2
    )
    assert retry_ai.calls == 2
    assert [page["file"] for page in pages] == [path.name for path in paths]
    with storage.connect(output) as database:
        assert database.execute("SELECT COUNT(*) FROM page_analyses").fetchone()[0] == 4


@pytest.mark.parametrize("error_type,status", [(AuthenticationError, 401), (PermissionDeniedError, 403)])
def test_account_errors_stop_new_pages_but_save_inflight_successes(tmp_path: Path, error_type, status):
    paths = [tmp_path / f"page-{number}.png" for number in range(1, 7)]
    output = tmp_path / "output"
    storage.initialize(output)
    storage.upsert_collection(output, "test", "Test", ".", tmp_path)
    run_id = _start_run(output, paths)
    barrier = threading.Barrier(2)
    calls = []

    class AccountErrorAI(FakeAI):
        def analyze_page(self, path):
            calls.append(path)
            barrier.wait(timeout=5)
            if path == paths[0]:
                raise error_type(
                    "access denied",
                    response=httpx.Response(status, request=httpx.Request("POST", "https://example.test")),
                    body=None,
                )
            return super().analyze_page(path)

    with pytest.raises(PageAnalysisError) as error:
        _load_or_analyze_pages(
            paths, output, "test", run_id, AccountErrorAI(), "test-model", workers=2
        )

    assert set(calls) == set(paths[:2])
    assert "1 failed, 1 successful/cached, 4 not attempted" in str(error.value)
    assert storage.load_cached_page(output, "test", 2, paths[1].name, _analysis_key()) is not None
    assert storage.load_cached_page(output, "test", 1, paths[0].name, _analysis_key()) is None


class BatchAI(BoundaryAI):
    def __init__(self, workers, fail_stage=None):
        super().__init__()
        self.split = True
        self.workers = workers
        self.fail_stage = fail_stage
        self.active = {"boundaries": 0, "labels": 0}
        self.maximum = dict(self.active)
        self.calls = {"boundaries": [], "labels": []}
        self.finished = {"boundaries": [], "labels": []}
        self.lock = threading.Lock()
        self.barriers = {stage: threading.Barrier(workers) for stage in self.active}
        self.second_finished = {stage: threading.Event() for stage in self.active}

    def request(self, stage, number, invoke):
        with self.lock:
            self.calls[stage].append(number)
            self.active[stage] += 1
            self.maximum[stage] = max(self.maximum[stage], self.active[stage])
        try:
            if len(self.calls[stage]) <= self.workers:
                self.barriers[stage].wait(timeout=5)
            if stage == self.fail_stage and number == 1:
                raise RuntimeError(f"{stage} failed")
            # Force the second request to finish before the first.
            if number == 1 and self.workers > 1:
                assert self.second_finished[stage].wait(timeout=5)
            time.sleep(0.03)
            value = invoke()
            self.finished[stage].append(number)
            if number == 2:
                self.second_finished[stage].set()
            return value
        finally:
            with self.lock:
                self.active[stage] -= 1

    def decide_boundaries(self, pages):
        assert self.active["labels"] == 0
        return self.request("boundaries", pages[0]["page"],
                            lambda: super(BatchAI, self).decide_boundaries(pages))

    def label_groups(self, groups, offset):
        assert self.active["boundaries"] == 0
        def labels():
            values = super(BatchAI, self).label_groups(groups, offset)
            for value, group in zip(values, groups):
                value["label"] = f"Document {group[0]['page']}"
            return values
        return self.request("labels", groups[0][0]["page"], labels)


@pytest.mark.parametrize("workers", [1, 2])
def test_batch_workers_preserve_order_and_write_only_on_main_thread(archive, monkeypatch, workers):
    caller = threading.get_ident()
    connect = storage.connect
    def checked_connect(output):
        assert threading.get_ident() == caller
        return connect(output)
    monkeypatch.setattr(storage, "connect", checked_connect)
    model = BatchAI(workers)
    run(archive, model, "--workers", str(workers), "--boundary-batch", "1", "--label-batch", "1")
    assert model.maximum == {"boundaries": workers, "labels": workers}
    if workers > 1:
        assert all(order.index(2) < order.index(1) for order in model.finished.values())
    data = storage.load_collection_data(archive[1], "default")
    assert [doc["label"] for doc in data["sections"]] == [f"Document {n}" for n in range(1, 6)]


@pytest.mark.parametrize("stage", ["boundaries", "labels"])
def test_batch_failure_saves_inflight_success_and_resumes(archive, stage):
    model = BatchAI(2, fail_stage=stage)
    options = ("--workers", "2", "--boundary-batch", "1", "--label-batch", "1")
    with pytest.raises(RuntimeError, match=f"{stage} failed"):
        run(archive, model, *options)
    assert sorted(model.calls[stage]) == [1, 2]
    with storage.connect(archive[1]) as db:
        assert db.execute("SELECT COUNT(*) FROM analysis_runs WHERE completed_at IS NOT NULL").fetchone()[0] == 0
    retry = BatchAI(1)
    run(archive, retry, *options, "--workers", "1")
    assert 2 not in retry.calls[stage]
    assert 1 in retry.calls[stage]
    if stage == "labels":
        assert retry.calls["boundaries"] == []
