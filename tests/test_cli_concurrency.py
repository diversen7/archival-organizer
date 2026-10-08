import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from openai import AuthenticationError, PermissionDeniedError

from archival_organizer import storage
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
