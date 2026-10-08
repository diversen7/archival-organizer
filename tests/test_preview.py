from pathlib import Path
import sys
from urllib.parse import quote

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from archival_organizer import preview, viewer


def make_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 60), "ivory").save(path)


def test_catalog_discovers_images_without_decoding_and_excludes_empty_branches(tmp_path, monkeypatch):
    source = tmp_path / "scans"
    source.mkdir()
    for name in ("scan10.png", "scan2.PNG", "scan1.tif", "notes.txt"):
        (source / name).touch()
    (source / "box" / "part1").mkdir(parents=True)
    (source / "box" / "part1" / "1.webp").touch()
    (source / "empty").mkdir()
    monkeypatch.setattr(Image, "open", lambda *_args: pytest.fail("Discovery must not decode images"))
    catalog = preview.load_preview_catalog(source)
    assert catalog.images["."] == ("scan1.tif", "scan2.PNG", "scan10.png")
    assert catalog.page_counts == {".": 3, "box/part1": 1}
    assert catalog.folders == {".", "box", "box/part1"}


def test_preview_browses_nested_images_and_originals_without_database_or_writes(tmp_path, monkeypatch):
    source = tmp_path / "scans"
    folder = "91+01011-3/part 1 Æ & # 50%"
    make_image(source / folder / "scan2.png")
    make_image(source / folder / "scan10.png")
    make_image(source / "91+01011-4" / "part1" / "1.png")
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(viewer.storage, "initialize", lambda *_: pytest.fail("No database in preview"))
    monkeypatch.setattr(viewer.storage, "connect", lambda *_: pytest.fail("No database in preview"))
    with monkeypatch.context() as no_decode:
        no_decode.setattr(Image, "open", lambda *_: pytest.fail("Startup must not decode images"))
        app = preview.create_preview_app(source)
    assert all(not (getattr(route, "methods", set()) & {"POST", "PUT", "DELETE", "PATCH"})
               for route in app.routes)
    url = f"/archive/{quote(folder, safe='/')}/"
    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert root.url.path == "/archive/"
        assert "Preview" in root.text
        assert 'href="/archive/91%2B01011-3/"' in root.text
        assert 'href="/archive/91%2B01011-4/"' in root.text
        assert "part 1" not in root.text
        parent = client.get("/archive/91%2B01011-3/")
        assert f'href="{url}"' in parent.text
        page = client.get(url)
        assert "Image 1 of 2 · scan2.png" in page.text
        assert "part 1 Æ &amp; # 50%" in page.text
        assert 'href="/archive/">Archive Top</a>' in page.text
        assert 'href="/archive/91%2B01011-3/">91+01011-3</a>' in page.text
        assert 'class="page-content" id="preview-image"' in page.text
        assert f'data-next-url="{url}?page=2#preview-image"' in page.text
        assert f'href="{url}?page=2#preview-image"' in page.text
        assert "Correct grouping" not in page.text
        assert "Extracted information" not in page.text
        assert "Refresh archive" not in page.text
        assert client.post("/refresh").status_code == 404
        assert "/review/" not in page.text
        assert "Image 2 of 2 · scan10.png" in client.get(url + "?page=2").text
        image = client.get(f"/images/1/{quote(folder, safe='/')}")
        assert image.headers["content-type"] == "image/jpeg"
        assert image.content.startswith(b"\xff\xd8")
        original = client.get(f"/original/1/{quote(folder, safe='/')}")
        assert original.content == (source / folder / "scan2.png").read_bytes()
        assert original.headers["content-type"] == "image/png"
        assert client.post("/collections/default/review/undo").status_code == 404
        assert client.get("/static/archive.js").status_code == 200
    after = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert before == after
    assert list(tmp_path.iterdir()) == [source]


def test_preview_folder_can_have_images_and_children(tmp_path):
    make_image(tmp_path / "root.png")
    make_image(tmp_path / "box" / "1.png")
    make_image(tmp_path / "box" / "images" / "2.png")
    with TestClient(preview.create_preview_app(tmp_path)) as client:
        root = client.get("/archive/")
        assert "Image 1 of 1 · root.png" in root.text
        assert 'href="/archive/box/"' in root.text
        assert client.get("/images/1/").status_code == 200
        assert client.get("/original/1/").status_code == 200
        box = client.get("/archive/box/")
        assert "Subfolders" in box.text
        assert 'href="/archive/box/images/"' in box.text
        assert "Image 1 of 1 · 1.png" in box.text
        assert "Image 1 of 1 · 2.png" in client.get("/archive/box/images/").text


def test_large_image_list_is_paginated_and_navigation_is_bounded(tmp_path, monkeypatch):
    for number in range(1, 206):
        (tmp_path / f"scan{number}.png").touch()
    monkeypatch.setattr(Image, "open", lambda *_: pytest.fail("HTML must not decode images"))
    with TestClient(preview.create_preview_app(tmp_path)) as client:
        first = client.get("/archive/")
        assert first.text.count('class="section image-link') == 100
        assert "scan101.png" not in first.text
        assert 'href="/archive/?page=101">Later images' in first.text
        assert 'data-previous-url=""' in first.text
        middle = client.get("/archive/?page=101")
        assert middle.text.count('class="section image-link') == 100
        assert "Image 101 of 205 · scan101.png" in middle.text
        assert 'href="/archive/?page=1">← Earlier images' in middle.text
        assert 'data-previous-url="/archive/?page=100#preview-image"' in middle.text
        assert 'href="/archive/?page=100#preview-image"' in middle.text
        last = client.get("/archive/?page=205")
        assert last.text.count('class="section image-link') == 5
        assert 'data-next-url=""' in last.text
        assert 'name="page" type="number" min="1" max="205"' in last.text
        assert client.get("/archive/?page=999", follow_redirects=False).headers["location"] == (
            "/archive/?page=205"
        )
        assert client.get("/archive/?page=0").status_code == 422


def test_missing_corrupt_and_uncataloged_images_return_not_found(tmp_path):
    make_image(tmp_path / "1.png")
    (tmp_path / "2.png").write_text("not an image")
    with TestClient(preview.create_preview_app(tmp_path)) as client:
        (tmp_path / "1.png").unlink()
        make_image(tmp_path / "new-folder" / "new.png")
        for url in ("/images/1/", "/original/1/", "/images/2/", "/images/3/", "/original/0/",
                    "/archive/missing/", "/archive/new-folder/", "/images/1/missing",
                    "/images/1/%2E%2E", "/original/1/%2Fetc"):
            assert client.get(url).status_code == 404


def test_symlinks_are_excluded_and_replacements_cannot_escape_source(tmp_path):
    source, outside = tmp_path / "scans", tmp_path / "outside"
    make_image(source / "box" / "1.png")
    make_image(outside / "1.png")
    (source / "external").symlink_to(outside, target_is_directory=True)
    (source / "linked.png").symlink_to(outside / "1.png")
    catalog = preview.load_preview_catalog(source)
    assert catalog.images == {"box": ("1.png",)}
    with TestClient(preview.create_preview_app(source)) as client:
        (source / "box" / "1.png").unlink()
        (source / "box").rmdir()
        (source / "box").symlink_to(outside, target_is_directory=True)
        assert client.get("/images/1/box").status_code == 404
        assert client.get("/original/1/box").status_code == 404


@pytest.mark.parametrize("missing", [False, True])
def test_preview_rejects_empty_or_missing_sources(tmp_path, missing):
    with pytest.raises(ValueError, match="does not exist" if missing else "No supported images"):
        preview.create_preview_app(tmp_path / "missing" if missing else tmp_path)


@pytest.mark.parametrize("preview_mode", [False, True])
def test_cli_selects_mode_and_preserves_server_options(tmp_path, monkeypatch, preview_mode):
    app = object()
    factory = []
    served = []
    monkeypatch.setattr(preview, "create_preview_app", lambda path: factory.append(("preview", path)) or app)
    monkeypatch.setattr(viewer, "create_app", lambda path, override: factory.append(("archive", path, override)) or app)
    monkeypatch.setattr(viewer.uvicorn, "run", lambda app, **kwargs: served.append((app, kwargs)))
    args = ["archival-browser", str(tmp_path), "--host", "0.0.0.0", "--port", "8002"]
    args += ["--preview"] if preview_mode else ["--input", str(tmp_path / "images")]
    monkeypatch.setattr(sys, "argv", args)
    viewer.main()
    assert factory == ([("preview", tmp_path)] if preview_mode else
                       [("archive", tmp_path, tmp_path / "images")])
    assert served == [(app, {"host": "0.0.0.0", "port": 8002})]


def test_cli_rejects_preview_with_input_before_loading(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(preview, "create_preview_app", lambda *_: pytest.fail("Must reject before loading"))
    monkeypatch.setattr(sys, "argv", [
        "archival-browser", str(tmp_path), "--preview", "--input", str(tmp_path),
    ])
    with pytest.raises(SystemExit) as error:
        viewer.main()
    assert error.value.code == 2
    assert "do not combine it with --input" in capsys.readouterr().err


def test_preview_restart_picks_up_added_images(tmp_path, monkeypatch):
    make_image(tmp_path / "box" / "2.png")
    monkeypatch.setattr(viewer.storage, "initialize", lambda *_: pytest.fail("No database in preview"))
    with TestClient(preview.create_preview_app(tmp_path)) as client:
        make_image(tmp_path / "box" / "1.png")
        assert "Image 1 of 1 · 2.png" in client.get("/archive/box/").text
        assert client.post("/refresh").status_code == 404
    with TestClient(preview.create_preview_app(tmp_path)) as client:
        assert "Image 2 of 2 · 2.png" in client.get("/archive/box/?file=2.png").text
        assert not (tmp_path / "archive.sqlite3").exists()
