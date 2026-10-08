from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import quote

import pytest

from fastapi.testclient import TestClient
from PIL import Image

from archival_organizer import storage
from archival_organizer.labeling import regenerate_collection_labels
from archival_organizer.viewer import (
    _change_boundary,
    _move_page,
    _preview,
    _render_collections,
    _folder_context,
    _render_page,
    _undo_boundary_change,
    WEB_DIR,
    templates,
    create_app,
    load_collections,
    load_run,
)


class FakeLabelAI:
    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def label_numbered_groups(self, groups):
        self.calls.append([number for number, _pages in groups])
        return [
            {
                "group": number,
                "label": f"Regenerated section {number}",
                "document_type": "report",
                "summary": "Regenerated summary",
                "date": "1960",
                "places": ["Viborg"],
                "subjects": ["civil defence"],
                "confidence": 0.94,
            }
            for number, _pages in groups
        ]


def _add_sample_collection(
    output: Path, images: Path, collection_id: str, relative_path: str = ".", *,
    label: str = "Test document",
) -> None:
    images.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 60), "white").save(images / "page 1.png")
    Image.new("RGB", (40, 60), "ivory").save(images / "page 2.png")
    storage.upsert_collection(output, collection_id, images.name, relative_path, images)
    paths = [images / "page 1.png", images / "page 2.png"]
    storage.sync_pages(output, collection_id, paths)
    run_id = storage.start_run(
        output,
        collection_id,
        "test-model",
        "test-analysis",
        {"page_analysis": "Analyze a page", "boundary": "Find boundaries"},
    )
    for number in (1, 2):
        storage.save_page_analysis(output, collection_id, run_id, {
            "page": number, "file": f"page {number}.png", "title": f"Page {number}",
            "summary": "Extracted summary", "document_type": "letter",
            "transcription": "Some <visible> text", "extraction_confidence": 0.92,
        }, "test-model", "test-analysis")
    storage.save_results(output, collection_id, [{
        "section": 1, "start_page": 1, "end_page": 2, "label": label,
        "document_type": "letter", "summary": "A summary", "date": "1956",
        "places": ["Aarhus"], "subjects": ["testing"], "label_confidence": 0.9,
        "lowest_boundary_confidence": 0.8,
    }], {2: {
        "right_page": 2, "starts_new_document": False, "confidence": 0.85,
        "reason": "The text continues.",
    }}, run_id)


def _sample_run(tmp_path: Path) -> Path:
    output = tmp_path / "output"
    storage.initialize(output)
    _add_sample_collection(output, tmp_path / "images", "default")
    return output


def _move_sample_run(tmp_path: Path) -> Path:
    output = tmp_path / "move-output"
    images = tmp_path / "move-images"
    images.mkdir()
    storage.initialize(output)
    paths = []
    for number in range(1, 6):
        path = images / f"page {number}.png"
        Image.new("RGB", (40, 60), "white").save(path)
        paths.append(path)
    storage.upsert_collection(output, "default", images.name, ".", images)
    storage.sync_pages(output, "default", paths)
    run_id = storage.start_run(output, "default", "test-model", "test-analysis", {})
    for number in range(1, 6):
        storage.save_page_analysis(output, "default", run_id, {
            "page": number, "file": f"page {number}.png", "title": f"Page {number}",
            "summary": "Summary", "document_type": "letter", "transcription": "Text",
            "extraction_confidence": 0.9,
        }, "test-model", "test-analysis")
    sections = [
        {
            "section": index, "start_page": start, "end_page": end, "label": label,
            "document_type": "letter", "summary": label, "date": "", "places": [],
            "subjects": [], "label_confidence": 0.9, "lowest_boundary_confidence": 0.8,
        }
        for index, (start, end, label) in enumerate(
            [(1, 2, "First"), (3, 4, "Second"), (5, 5, "Third")], start=1
        )
    ]
    boundaries = {
        number: {
            "right_page": number,
            "starts_new_document": number in {3, 5},
            "confidence": 0.8,
            "reason": "Original grouping",
        }
        for number in range(2, 6)
    }
    storage.save_results(output, "default", sections, boundaries, run_id)
    return output


def test_load_run_maps_pages_to_sections(tmp_path: Path):
    run = load_run(_sample_run(tmp_path))
    assert run.page_count == 2
    assert run.page_sections[2]["label"] == "Test document"
    assert run.boundaries[2]["starts_new_document"] is False


def test_viewer_renders_page_metadata_and_images(tmp_path: Path):
    output = _sample_run(tmp_path)
    run = load_run(output)
    rendered = _render_page(run, 2)
    assert "Test document" in rendered
    assert "Some &lt;visible&gt; text" in rendered
    assert "Continues previous page" in rendered
    assert "Label review" not in rendered
    assert "Grouping review" not in rendered
    assert 'id="page-strip"' in rendered
    assert 'data-previous-url="/?page=1#page-strip"' in rendered
    first_page = _render_page(run, 1)
    assert 'data-next-url="/?page=2#page-strip"' in first_page
    assert 'href="/?page=2#page-strip"' in first_page
    asset_version = templates.env.globals["asset_version"]
    assert f'<link rel="stylesheet" href="/static/archive.css?v={asset_version}">' in rendered
    assert f'<script src="/static/archive.js?v={asset_version}" defer></script>' in rendered
    assert "activeSection.getBoundingClientRect().top" in (
        WEB_DIR / "static" / "archive.js"
    ).read_text(encoding="utf-8")
    assert ".section[hidden], .collection[hidden]" in (
        WEB_DIR / "static" / "archive.css"
    ).read_text(encoding="utf-8")

    image_path = run.input_dir / "page 2.png"
    preview = _preview(str(image_path), image_path.stat().st_mtime_ns, 320)
    assert preview.startswith(b"\xff\xd8")
    app = create_app(output)
    assert {route.path for route in app.routes} >= {
        "/", "/collections/{collection_id}/",
        "/collections/{collection_id}/images/{page_number}",
        "/collections/{collection_id}/images/{page_number}/original",
        "/collections/{collection_id}/review/boundaries/{right_page}/split",
        "/collections/{collection_id}/review/boundaries/{right_page}/join",
        "/collections/{collection_id}/review/pages/{page_number}/move",
        "/collections/{collection_id}/review/undo",
    }
    with TestClient(app) as client:
        index_response = client.get("/")
        assert index_response.status_code == 200
        assert index_response.url.path == "/archive/"
        assert "Archive Top" in index_response.text
        assert "Test document" in index_response.text
        page_response = client.get("/collections/default/")
        assert page_response.status_code == 200
        assert page_response.url.path == "/archive/"
        assert "Some &lt;visible&gt; text" in page_response.text
        assert 'href="/original/1/"' in page_response.text
        assert client.get("/original/1/").headers["content-type"] == "image/png"
        assert client.get("/static/archive.css").status_code == 200
        assert client.get("/static/archive.js").status_code == 200


def test_reviewer_can_split_and_undo_while_preserving_ai_decision(tmp_path: Path):
    output = _sample_run(tmp_path)
    run = load_run(output)
    reviewed = _change_boundary(output, run.input_dir, 2, True)
    assert len(reviewed.sections) == 2
    assert reviewed.boundaries[2]["review_source"] == "human"
    assert reviewed.original_boundaries[2]["starts_new_document"] is False
    assert reviewed.sections[0]["review_adjusted"] is True
    assert reviewed.sections[1]["label_needs_review"] is True
    reviewed_page = _render_page(reviewed, 1)
    assert "Adjusted" in reviewed_page
    assert "Label review" in reviewed_page
    assert "Grouping review" not in reviewed_page
    assert "Regenerate changed section metadata" not in _render_page(reviewed, 1)
    assert (output / "reviewed-sections.csv").is_file()
    assert "human_adjusted" in (output / "reviewed-sections.csv").read_text(encoding="utf-8")

    restored = _undo_boundary_change(output, run.input_dir)
    assert len(restored.sections) == 1
    assert restored.boundary_overrides == {}
    assert restored.sections[0]["label"] == "Test document"


def test_reviewer_can_move_page_to_distant_section_and_undo(tmp_path: Path):
    output = _move_sample_run(tmp_path)
    original = load_run(output)

    moved = _move_page(output, original.input_dir, 2, 4)

    assert [page["file"] for page in moved.pages.values()] == [
        "page 1.png", "page 3.png", "page 4.png", "page 2.png", "page 5.png"
    ]
    assert [(section["start_page"], section["end_page"]) for section in moved.sections] == [
        (1, 1), (2, 4), (5, 5)
    ]
    assert moved.page_sections[4]["label"] == "Second"
    assert moved.page_sections[4]["review_adjusted"] is True
    assert (output / "reviewed-sections.csv").is_file()

    restored = _undo_boundary_change(output, original.input_dir)
    assert [page["file"] for page in restored.pages.values()] == [
        f"page {number}.png" for number in range(1, 6)
    ]
    assert [(section["start_page"], section["end_page"]) for section in restored.sections] == [
        (1, 2), (3, 4), (5, 5)
    ]


def test_move_page_form_and_route(tmp_path: Path):
    output = _move_sample_run(tmp_path)
    app = create_app(output)

    with TestClient(app) as client:
        page = client.get("/collections/default/?page=2")
        assert 'action="/collections/default/review/pages/2/move"' in page.text
        assert 'name="target_page"' in page.text
        assert (
            page.text.index('class="page-strip"')
            < page.text.index('class="viewer"')
            < page.text.index('class="image-card"')
            < page.text.index('class="card review-controls"')
            < page.text.index('Extracted information')
        )

        response = client.post(
            "/collections/default/review/pages/2/move",
            data={"target_page": "4"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/archive/?page=4"
        assert load_run(output).pages[4]["file"] == "page 2.png"


def test_viewer_distinguishes_grouping_review(tmp_path: Path):
    run = load_run(_sample_run(tmp_path))
    run.sections[0]["lowest_boundary_confidence"] = 0.6

    rendered = _render_page(run, 1)

    assert "Grouping review" in rendered
    assert "Label review" not in rendered


def test_regenerates_only_changed_group_metadata_and_reuses_exact_membership(tmp_path: Path):
    output = _sample_run(tmp_path)
    run = load_run(output)
    _change_boundary(output, run.input_dir, 2, True)
    ai = FakeLabelAI()

    assert regenerate_collection_labels(output, "default", ai=ai) == (2, 2)
    regenerated = load_run(output)

    assert ai.calls == [[1, 2]]
    assert [section["label"] for section in regenerated.sections] == [
        "Regenerated section 1", "Regenerated section 2"
    ]
    assert all(not section["label_needs_review"] for section in regenerated.sections)
    assert all(section["label_source"] == "regenerated" for section in regenerated.sections)

    joined = _change_boundary(output, run.input_dir, 2, False)
    assert joined.sections[0]["label"] == "Test document"
    split_again = _change_boundary(output, run.input_dir, 2, True)
    assert [section["label"] for section in split_again.sections] == [
        "Regenerated section 1", "Regenerated section 2"
    ]

    paths = [run.input_dir / "page 1.png", run.input_dir / "page 2.png"]
    storage.sync_pages(output, "default", paths)
    rerun_id = storage.start_run(output, "default", "test-model", "test-analysis", {})
    for number, path in enumerate(paths, start=1):
        storage.save_page_analysis(output, "default", rerun_id, {
            "page": number, "file": path.name, "title": f"Page {number}",
            "summary": "Extracted summary", "document_type": "letter",
            "transcription": "Some visible text", "extraction_confidence": 0.92,
        }, "test-model", "test-analysis")
    storage.save_results(output, "default", [{
        "section": 1, "start_page": 1, "end_page": 2, "label": "Fresh AI label",
        "document_type": "letter", "summary": "A summary", "date": "1956",
        "places": ["Aarhus"], "subjects": ["testing"], "label_confidence": 0.9,
        "lowest_boundary_confidence": 0.8,
    }], {2: {
        "right_page": 2, "starts_new_document": False, "confidence": 0.85,
        "reason": "The text continues.",
    }}, rerun_id)

    after_rerun = load_run(output)
    assert after_rerun.boundaries[2]["review_source"] == "human"
    assert [section["label"] for section in after_rerun.sections] == [
        "Regenerated section 1", "Regenerated section 2"
    ]


def test_multi_collection_viewer_has_picker_and_scoped_routes(tmp_path: Path):
    library = tmp_path / "library"
    storage.initialize(library)
    _add_sample_collection(library, tmp_path / "box 1" / "folder 2", "alpha", "box 1/folder 2")
    _add_sample_collection(library, tmp_path / "box 1" / "folder 10", "beta", "box 1/folder 10")

    collections = load_collections(library)
    landing = _render_collections(collections)
    rendered = _render_page(
        collections[0].run, 1, base_url="/archive/box%201/folder%202",
        content_url="/collections/alpha",
        collection_path=collections[0].relative_path,
        folder_context=_folder_context(collections, collections[0].relative_path),
    )
    assert "Choose a folder" in landing
    assert 'href="/archive/box%201/"' in landing
    assert "folder 2" not in landing
    assert f'href="/static/archive.css?v={templates.env.globals["asset_version"]}"' in landing
    assert 'src="/collections/alpha/images/1"' in rendered
    assert 'href="/archive/">Archive Top</a>' in rendered
    assert 'href="/archive/box%201/">box 1</a>' in rendered
    assert '<span aria-current="page">folder 2</span>' in rendered
    app = create_app(library)
    assert {route.path for route in app.routes} >= {
        "/", "/collections/{collection_id}/",
        "/collections/{collection_id}/images/{page_number}",
        "/collections/{collection_id}/images/{page_number}/original",
        "/collections/{collection_id}/review/boundaries/{right_page}/split",
        "/collections/{collection_id}/review/boundaries/{right_page}/join",
        "/collections/{collection_id}/review/pages/{page_number}/move",
        "/collections/{collection_id}/review/undo",
    }

    _change_boundary(library, collections[0].run.input_dir, 2, True, "alpha")
    assert load_run(library, collection_id="alpha").boundary_overrides
    assert not load_run(library, collection_id="beta").boundary_overrides


def test_browser_navigates_hierarchy_and_redirects_old_links(tmp_path: Path):
    output, source = tmp_path / "archive", tmp_path / "scans"
    storage.initialize(output)
    for collection_id, path in [
        ("a", "91+01011-3/part2"), ("b", "91+01011-3/part10"),
        ("c", "91+01011-4/part1"),
    ]:
        _add_sample_collection(output, source / path, collection_id, path)
    (source / "not-analyzed").mkdir()
    with TestClient(create_app(output)) as client:
        root = client.get("/archive/")
        assert root.status_code == 200
        assert 'href="/archive/91%2B01011-3/"' in root.text
        assert 'href="/archive/91%2B01011-4/"' in root.text
        assert "part2" not in root.text
        assert "not-analyzed" not in root.text
        assert "4 pages · 2 collections" in root.text

        parent = client.get("/archive/91%2B01011-3/")
        assert parent.status_code == 200
        assert parent.text.index(">part2</strong>") < parent.text.index(">part10</strong>")
        assert 'href="/archive/">Archive Top</a>' in parent.text
        assert "91+01011-4" not in parent.text

        page = client.get("/archive/91%2B01011-3/part2/?page=2")
        assert page.status_code == 200
        assert 'href="/archive/91%2B01011-3/">91+01011-3</a>' in page.text
        assert '<span aria-current="page">part2</span>' in page.text
        assert 'href="/archive/91%2B01011-3/part2/?page=1#page-strip"' in page.text
        legacy = client.get("/collections/a/?page=2", follow_redirects=False)
        assert legacy.headers["location"] == "/archive/91%2B01011-3/part2/?page=2"
        overflow = client.get("/archive/91%2B01011-3/part2/?page=99", follow_redirects=False)
        assert overflow.headers["location"] == "/archive/91%2B01011-3/part2/?page=2"
        for url in ["/archive/missing/", "/archive/91%2B01011-3/part1/",
                    "/original/1/91%2B01011-3", "/original/1/missing",
                    "/archive/91%2B01011-3/%2E%2E/not-analyzed/"]:
            assert client.get(url).status_code == 404


def test_readable_paths_handle_special_characters_and_review_actions(tmp_path: Path):
    output = tmp_path / "archive"
    path = "91+01011-3/part 1 Æ & # 50%"
    url = f"/archive/{quote(path, safe='/')}/"
    storage.initialize(output)
    _add_sample_collection(output, tmp_path / "scans" / path, "special", path)
    _add_sample_collection(output, tmp_path / "scans" / "other", "other", "other")
    with TestClient(create_app(output)) as client:
        page = client.get(url)
        assert page.status_code == 200
        assert "part 1 Æ &amp; # 50%" in page.text
        original_url = f"/original/1/{quote(path, safe='/')}"
        assert f'href="{original_url}"' in page.text
        assert client.get(original_url).headers["content-type"] == "image/png"
        assert client.get(f"/original/99/{quote(path, safe='/')}").status_code == 404
        assert client.get("/collections/special/images/1").headers["content-type"] == "image/jpeg"
        for action in ("split", "join"):
            response = client.post(
                f"/collections/special/review/boundaries/2/{action}", follow_redirects=False,
            )
            assert response.headers["location"] == url + "?page=2"
            assert client.get(response.headers["location"]).status_code == 200
        moved = client.post(
            "/collections/special/review/pages/1/move", data={"target_page": "2"},
            follow_redirects=False,
        )
        assert moved.headers["location"] == url + "?page=2"
        assert load_run(output, collection_id="special").pages[1]["file"] == "page 2.png"
        undo = client.post("/collections/special/review/undo?page=2", follow_redirects=False)
        assert undo.headers["location"] == url + "?page=2"
        assert load_run(output, collection_id="special").pages[1]["file"] == "page 1.png"
        assert not load_run(output, collection_id="other").boundary_overrides


def test_folder_with_own_images_also_exposes_child_folders(tmp_path: Path):
    output, source = tmp_path / "archive", tmp_path / "scans"
    storage.initialize(output)
    _add_sample_collection(output, source, "root")
    _add_sample_collection(output, source / "box", "box", "box")
    _add_sample_collection(output, source / "box" / "images" / "1", "child", "box/images/1")
    with TestClient(create_app(output)) as client:
        root = client.get("/archive/")
        assert "Test document" in root.text
        assert 'href="/archive/box/"' in root.text
        box = client.get("/archive/box/")
        assert "Test document" in box.text
        assert "Subfolders" in box.text
        assert 'href="/archive/box/images/"' in box.text
        assert client.get("/archive/box/images/").status_code == 200
        assert "Test document" in client.get("/archive/box/images/1/").text


@pytest.mark.parametrize("path", ["../outside", "/absolute", "box/../outside", "box//part"])
def test_browser_rejects_invalid_stored_collection_paths(tmp_path: Path, path: str):
    output = tmp_path / "archive"
    storage.initialize(output)
    _add_sample_collection(output, tmp_path / "images", "invalid", path)
    with pytest.raises(ValueError, match="Invalid collection path"):
        create_app(output)


def _view_link(html: str, label: str) -> str:
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.href = None
            self.found = None

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.href = dict(attrs).get("href")

        def handle_endtag(self, tag):
            if tag == "a":
                self.href = None

        def handle_data(self, data):
            if self.href is not None and data == label:
                self.found = self.href

    links = Links()
    links.feed(html)
    assert links.found is not None, f"Link missing: {label}"
    return links.found


def test_combined_switch_preserves_folder_and_filename_after_review_reordering(tmp_path):
    output, source = tmp_path / "output", tmp_path / "scans"
    path = "91+01011-3/part Æ & # 50%"
    encoded = quote(path, safe="/")
    storage.initialize(output)
    _add_sample_collection(output, source / path, "a", path)
    storage.move_page_after(output, "a", 1, 2)
    with TestClient(create_app(output, source)) as client:
        analyzed = client.get(f"/archive/{encoded}/?page=1")
        assert "Page 1 · page 2.png" in analyzed.text
        assert '>Analyzed</a>' in analyzed.text
        raw = client.get(_view_link(analyzed.text, "Preview"))
        assert raw.status_code == 200
        assert "Image 2 of 2 · page 2.png" in raw.text
        assert 'href="/preview/91%2B01011-3/"' in raw.text
        assert 'data-previous-url="/preview/' in raw.text
        assert f'src="/preview-images/2/{encoded}"' in raw.text
        assert "Correct grouping" not in raw.text
        assert "/review/" not in raw.text
        assert client.get(f"/preview-original/2/{encoded}").content == (source / path / "page 2.png").read_bytes()
        assert client.get(f"/preview-images/2/{encoded}").headers["cache-control"] == "private, no-cache"
        back = client.get(_view_link(raw.text, "Analyzed"))
        assert "Page 1 · page 2.png" in back.text
        assert "Correct grouping" in back.text
        parent = client.get("/preview/91%2B01011-3/")
        assert _view_link(parent.text, "Analyzed") == "/archive/91%2B01011-3/"
        for url in ("/preview/missing/", "/archive/missing/", "/preview-original/1/missing"):
            assert client.get(url).status_code == 404


def test_combined_unanalyzed_folders_and_images_offer_preview_without_wrong_page(tmp_path):
    output, source = tmp_path / "output", tmp_path / "scans"
    storage.initialize(output)
    _add_sample_collection(output, source / "done", "a", "done")
    (source / "pending" / "part1").mkdir(parents=True)
    Image.new("RGB", (40, 60)).save(source / "pending" / "part1" / "1.png")
    extra = "page 3 + &#.png"
    Image.new("RGB", (40, 60)).save(source / "done" / extra)
    with TestClient(create_app(output, source)) as client:
        raw = client.get("/preview/pending/part1/")
        missing = client.get(_view_link(raw.text, "Analyzed"))
        assert missing.status_code == 200
        assert "No completed analysis for this folder yet." in missing.text
        assert "Correct grouping" not in missing.text
        assert "Image 1 of 1" in client.get(_view_link(missing.text, "Back to Preview")).text
        parent = client.get("/archive/pending/")
        assert "No completed analysis" in parent.text
        partial = client.get("/preview/done/?page=3")
        missing_image = client.get(_view_link(partial.text, "Analyzed"))
        assert "This image has not been analyzed" in missing_image.text
        assert "Extracted information" not in missing_image.text
        assert "Image 3 of 3" in client.get(_view_link(missing_image.text, "Preview")).text
        assert _view_link(missing_image.text, "Open analyzed pages in this folder") == "/archive/done/"


def test_restart_picks_up_new_scans_completed_analyses_and_preserves_reviews(tmp_path):
    output, source = tmp_path / "output", tmp_path / "scans"
    storage.initialize(output)
    _add_sample_collection(output, source / "done", "a", "done")
    storage.set_boundary_override(output, "a", 2, True)
    with TestClient(create_app(output, source)) as client:
        _add_sample_collection(output, source / "new", "b", "new")
        Image.new("RGB", (40, 60), "black").save(source / "done" / "page 0.png")
        assert client.get("/archive/new/").status_code == 404
        assert client.get("/preview/new/").status_code == 404
        assert client.post("/refresh").status_code == 404
        for url in ("/archive/done/", "/preview/done/"):
            assert "Refresh archive" not in client.get(url).text
    with TestClient(create_app(output, source)) as client:
        assert "Image 3 of 3 · page 2.png" in client.get("/preview/done/?file=page+2.png").text
        assert client.get("/archive/new/").status_code == 200
        assert client.get("/preview/new/").status_code == 200
        assert "Adjusted" in client.get("/archive/done/").text
        assert load_run(output, collection_id="a").boundary_overrides
        with storage.connect(output) as database:
            assert database.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 2


def test_combined_browser_handles_archive_before_first_completed_analysis(tmp_path):
    output, source = tmp_path / "output", tmp_path / "scans"
    storage.initialize(output)
    source.mkdir()
    Image.new("RGB", (40, 60)).save(source / "1.png")
    with TestClient(create_app(output, source)) as client:
        landing = client.get("/")
        assert "No completed analysis" in landing.text
        assert "Image 1 of 1" in client.get(_view_link(landing.text, "Preview")).text
        _add_sample_collection(output, source / "new", "a", "new")
    with TestClient(create_app(output, source)) as client:
        assert "Test document" in client.get("/archive/new/").text


def test_analysis_only_restart_loads_latest_completed_run(tmp_path):
    output = _sample_run(tmp_path)
    with TestClient(create_app(output)) as client:
        _add_sample_collection(output, tmp_path / "images", "default", label="Updated analysis")
        assert "Updated analysis" not in client.get("/archive/").text
        assert "Refresh archive" not in client.get("/archive/").text
        assert client.post("/refresh").status_code == 404
    with TestClient(create_app(output)) as client:
        response = client.get("/archive/?file=page+2.png")
        assert "Updated analysis" in response.text
        assert "Page 2 · page 2.png" in response.text
        assert client.get("/preview/").status_code == 404


def test_combined_missing_parent_analysis_and_missing_preview_image(tmp_path):
    output, source = tmp_path / "output", tmp_path / "scans"
    storage.initialize(output)
    _add_sample_collection(output, source / "box" / "child", "child", "box/child")
    Image.new("RGB", (40, 60)).save(source / "box" / "unprocessed.png")
    with TestClient(create_app(output, source)) as client:
        raw = client.get("/preview/box/")
        analyzed = client.get(_view_link(raw.text, "Analyzed"))
        assert "No completed analysis" in analyzed.text
        assert 'href="/archive/box/child/"' in analyzed.text
        missing = client.get("/preview/box/?file=gone.png")
        assert "not present in the current preview catalog" in missing.text
        assert _view_link(missing.text, "Open source images in this folder") == "/preview/box/"


def test_combined_root_collection_switches_and_serves_source_images(tmp_path):
    output = _sample_run(tmp_path)
    with TestClient(create_app(output, tmp_path / "images")) as client:
        raw = client.get(_view_link(client.get("/archive/?page=2").text, "Preview"))
        assert "Image 2 of 2 · page 2.png" in raw.text
        assert client.get("/preview-original/2/").status_code == 200
        assert client.get("/preview-images/2/").status_code == 200
        assert "Page 2 · page 2.png" in client.get(_view_link(raw.text, "Analyzed")).text


def test_asset_version_reads_checkout_and_falls_back_to_package(tmp_path, monkeypatch):
    from archival_organizer import viewer

    monkeypatch.setattr(viewer, "WEB_DIR", tmp_path / "archival_organizer" / "web")
    monkeypatch.setattr(viewer, "version", lambda name: "0.1.0")
    assert viewer._asset_version() == "0.1.0"
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text('[project]\nversion = "1.0.1"\n')
    assert viewer._asset_version() == "1.0.1"


def test_versioned_assets_are_shared_and_served(tmp_path, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "asset_version", "1.0.1")
    output = _sample_run(tmp_path)
    with TestClient(create_app(output, tmp_path / "images")) as client:
        for url in ("/archive/", "/preview/"):
            response = client.get(url)
            assert '/static/archive.css?v=1.0.1' in response.text
            assert '/static/archive.js?v=1.0.1' in response.text
        assert client.get('/static/archive.css?v=1.0.1').status_code == 200
        assert client.get('/static/archive.js?v=1.0.1').status_code == 200
        monkeypatch.setitem(templates.env.globals, "asset_version", "1.0.2")
        response = client.get('/archive/')
        assert '/static/archive.css?v=1.0.2' in response.text
        assert '/static/archive.js?v=1.0.2' in response.text
