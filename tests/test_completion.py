import sqlite3

import pytest
from fastapi.testclient import TestClient

from archival_organizer import storage
from archival_organizer.viewer import create_app, load_run
from test_viewer import _add_sample_collection, _sample_run, _visible_text


def mark_done(output, collection_id="default", reviewer="Alice"):
    run_id = storage.load_collection_data(output, collection_id)["run_id"]
    storage.set_collection_done(output, collection_id, True, reviewer, run_id=run_id)
    return run_id


def test_completion_controls_persist_and_record_named_history(tmp_path):
    output = _sample_run(tmp_path)
    run_id = load_run(output).run_id
    url = f"/collections/default/review/completion/done?run_id={run_id}&page=2"
    with TestClient(create_app(output)) as client:
        before = client.get("/archive/").text
        assert "In progress · Images in this folder" in before
        assert "Mark as done" in before
        assert client.post(url).status_code == 400
        assert client.get(url).status_code == 405
        assert client.post(url, data={"reviewer_name": " "}).status_code == 400
        assert client.post(url, data={"reviewer_name": "A" * 101}).status_code == 400
        response = client.post(url, data={"reviewer_name": " Åse <reviewer> "}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/archive/?page=2"
        page = client.get(response.headers["location"]).text
        assert "✓ Done · Images in this folder" in page
        assert "Åse &lt;reviewer&gt;" in page
        assert ">Reopen</button>" in page
        # Repeated explicit requests must not toggle status or duplicate history.
        client.post(url, data={"reviewer_name": "Bob"})
        assert len(storage.load_edit_history(output, "default")) == 1

    with TestClient(create_app(output)) as client:
        assert "✓ Done" in client.get("/archive/").text
        response = client.post(url.replace("/done?", "/reopen?"), headers={"X-Reviewer-Name": "Bob"})
        assert response.status_code == 200
        assert "Reopened for further review." in response.text
        assert "Mark as done" in response.text
        client.post(url.replace("/done?", "/reopen?"), data={"reviewer_name": "Bob"})
        assert "Marked collection as done" in client.get("/collections/default/history").text
    edits = storage.load_edit_history(output, "default")
    assert [(e["action"], e["reviewer_name"]) for e in edits] == [
        ("reopen", "Bob"), ("done", "Åse <reviewer>"),
    ]


def test_nested_progress_includes_own_images_and_only_analyzed_collections(tmp_path):
    output, source = tmp_path / "archive", tmp_path / "scans"
    storage.initialize(output)
    for cid, path in [("own", "box"), ("a", "box/child"), ("b", "box/nested/child"),
                      ("other", "box-other/child")]:
        _add_sample_collection(output, source / path, cid, path)
    pending = source / "box/pending"
    pending.mkdir()
    (pending / "1.png").write_bytes(b"unprocessed")
    storage.upsert_collection(output, "pending", "Pending", "box/pending", pending)
    storage.start_run(output, "pending", "model", "key", {})

    with TestClient(create_app(output)) as client:
        mark_done(output, "a")
        mark_done(output, "b")
        root = client.get("/archive/").text
        assert "2 of 3 done" in _visible_text(root)
        assert "0 of 1 done" in _visible_text(root)  # sibling path stays independent
        assert "pending" not in root
        own = client.get("/archive/box/").text
        assert "In progress · Images in this folder" in own
        assert "2 of 3 analyzed collections done" in own
        assert "All 1 analyzed collection done" in client.get("/archive/box/nested/").text
        own_run = mark_done(output, "own")
        assert "✓ 3 of 3 done" in _visible_text(client.get("/archive/").text)
        assert "All 3 analyzed collections done" in client.get("/archive/box/").text
        storage.set_collection_done(output, "own", False, "Alice", run_id=own_run)
        assert "2 of 3 done" in _visible_text(client.get("/archive/").text)
        mark_done(output, "own")
        child_run = load_run(output, collection_id="b").run_id
        client.post(f"/collections/b/review/completion/reopen?run_id={child_run}",
                    data={"reviewer_name": "Alice"})
        assert "2 of 3 done" in _visible_text(client.get("/archive/").text)
        assert "0 of 1 analyzed collection done" in client.get("/archive/box/nested/").text
        mark_done(output, "b")

    _add_sample_collection(output, pending, "pending", "box/pending")
    with TestClient(create_app(output)) as client:
        assert "3 of 4 done" in _visible_text(client.get("/archive/").text)
        assert "3 of 4 analyzed collections done" in client.get("/archive/box/").text


@pytest.mark.parametrize("action", ["split", "join", "move", "undo", "label", "flag", "unflag"])
def test_review_changes_reopen_completion_atomically(tmp_path, action):
    output = _sample_run(tmp_path)
    storage.set_boundary_override(output, "default", 2, True, "Alice")
    data = storage.load_collection_data(output, "default")
    page_id = data["pages"][0]["page_id"]
    if action == "unflag":
        storage.set_document_flag(output, "default", [page_id], True, "Alice")
    mark_done(output)
    if action in {"split", "join"}:
        storage.set_boundary_override(output, "default", 2, action == "split", "Bob")
    elif action == "move":
        storage.move_page_after(output, "default", 1, 2, "Bob")
    elif action == "undo":
        storage.undo_boundary_override(output, "default", "Bob")
    elif action == "label":
        storage.save_reviewed_label(output, "default", [page_id], {
            "label": "Updated", "document_type": "letter", "summary": "", "date": "",
            "places": [], "subjects": [], "confidence": 0.9,
        }, "Bob")
    else:
        storage.set_document_flag(output, "default", [page_id], action == "flag", "Bob")
    status = storage.load_collection_completions(output)["default"]
    assert status["completed_at"] is None
    assert status["completed_by"] is None
    assert "Review changes" in status["reopen_reason"]
    history = storage.load_edit_history(output, "default")
    assert [e["action"] for e in history[:2]] == ["reopen", action]
    assert history[0]["reviewer_name"] == "Bob"
    with TestClient(create_app(output)) as client:
        assert status["reopen_reason"] in client.get("/archive/").text


def test_new_analysis_reopens_and_stale_completion_is_rejected(tmp_path):
    output = _sample_run(tmp_path)
    old_run = mark_done(output)
    with TestClient(create_app(output)) as client:
        storage.start_run(output, "default", "unfinished", "key", {})
        assert "✓ Done" in client.get("/archive/").text
        _add_sample_collection(output, tmp_path / "images", "default")
        status = storage.load_collection_completions(output)["default"]
        assert status["completed_at"] is None
        assert "new analysis" in status["reopen_reason"]
        assert status["reopen_reason"] in client.get("/archive/").text
        response = client.post(f"/collections/default/review/completion/done?run_id={old_run}",
                               data={"reviewer_name": "Alice"})
        assert response.status_code == 409
        assert "analysis has changed" in response.text
    assert storage.load_edit_history(output, "default")[0]["reviewer_name"] == "System"
    assert mark_done(output) != old_run
    assert storage.load_collection_completions(output)["default"]["reopen_reason"] == ""


@pytest.mark.parametrize("operation", ["done", "reopen", "edit", "analysis"])
def test_completion_and_history_failures_roll_back_together(tmp_path, operation):
    output = _sample_run(tmp_path)
    run_id = load_run(output).run_id
    if operation != "done":
        mark_done(output)
    with storage.connect(output) as database:
        database.execute("""CREATE TRIGGER reject_completion_history BEFORE INSERT ON edit_history
            WHEN NEW.action IN ('done', 'reopen')
            BEGIN SELECT RAISE(ABORT, 'history failed'); END""")
    before = storage.load_collection_data(output, "default")
    completions = storage.load_collection_completions(output)
    history = storage.load_edit_history(output, "default")
    with pytest.raises(sqlite3.IntegrityError, match="history failed"):
        if operation == "edit":
            storage.set_boundary_override(output, "default", 2, True, "Bob")
        elif operation == "analysis":
            _add_sample_collection(output, tmp_path / "images", "default")
        else:
            storage.set_collection_done(output, "default", operation == "done", "Bob", run_id=run_id)
    assert storage.load_collection_data(output, "default") == before
    assert storage.load_collection_completions(output) == completions
    assert storage.load_edit_history(output, "default") == history


def test_completion_rejects_unknown_or_unanalyzed_collections(tmp_path):
    output = _sample_run(tmp_path)
    storage.upsert_collection(output, "pending", "Pending", "pending", tmp_path)
    with TestClient(create_app(output)) as client:
        for cid in ("missing", "pending"):
            response = client.post(f"/collections/{cid}/review/completion/done?run_id=1",
                                   data={"reviewer_name": "Alice"})
            assert response.status_code == 404
            with pytest.raises(ValueError, match="Completed collection not found"):
                storage.set_collection_done(output, cid, True, "Alice", run_id=1)
