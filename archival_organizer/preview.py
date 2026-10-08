"""Read-only browsing of a filesystem snapshot, without persisted archive state."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .core import SUPPORTED_SUFFIXES, natural_key
from .viewer import (
    _archive_url,
    _browser_app,
    _browser_controls,
    _navigation_context,
    _original_image_response,
    _preview_image_response,
    templates,
)


IMAGE_LIST_SIZE = 100


@dataclass(frozen=True)
class PreviewCatalog:
    root: Path
    images: dict[str, tuple[str, ...]]
    page_counts: dict[str, int]
    folders: frozenset[str]

    def image_path(self, folder: str, page: int) -> Path:
        filenames = self.images.get(folder, ())
        if not 1 <= page <= len(filenames):
            raise HTTPException(status_code=404, detail="Image not found")
        path = self.root / folder / filenames[page - 1]
        # Recheck at request time: a cataloged directory may have been replaced by a symlink.
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=404, detail="Image not found") from exc
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise HTTPException(status_code=404, detail="Image not found")
        return resolved


def load_preview_catalog(source: Path, *, allow_empty: bool = False) -> PreviewCatalog:
    root = source.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Image directory does not exist: {root}")
    images: dict[str, tuple[str, ...]] = {}
    folders = {"."}

    def scan_error(error: OSError) -> None:
        raise error

    # One traversal; image content is never opened during discovery. Directory symlinks
    # are not followed and file symlinks are excluded from the snapshot.
    for directory, _subdirs, filenames in os.walk(root, onerror=scan_error, followlinks=False):
        parent = Path(directory)
        names = [
            name for name in filenames
            if Path(name).suffix.lower() in SUPPORTED_SUFFIXES
            and not (parent / name).is_symlink() and (parent / name).is_file()
        ]
        if not names:
            continue
        relative = parent.relative_to(root).as_posix()
        images[relative] = tuple(sorted(names, key=lambda name: natural_key(Path(name))))
        folders.add(relative)
        folders.update(ancestor.as_posix() for ancestor in PurePosixPath(relative).parents)
    if not images and not allow_empty:
        raise ValueError(f"No supported images found in {root} or its subdirectories")
    return PreviewCatalog(root, images, {path: len(names) for path, names in images.items()},
                          frozenset(folders))


def render_preview(
    catalog: PreviewCatalog, path: str, page: int, filename: str | None = None, *,
    combined: bool = False,
) -> Response:
    prefix = "/preview" if combined else "/archive"
    image_base = "/preview-images" if combined else "/images"
    original_base = "/preview-original" if combined else "/original"
    context = _navigation_context(catalog.page_counts, path, prefix)
    filenames = catalog.images.get(path, ())
    if filename is not None and filename in filenames:
        page = filenames.index(filename) + 1
    elif filename is not None:
        context["notice"] = "This image is not present in the current preview catalog."
        if filenames:
            context["preview_pages_url"] = _archive_url(path, prefix)
        filenames = ()
    selected_file = filenames[min(page, len(filenames)) - 1] if filenames else filename
    context["browser_controls"] = _browser_controls(path, "preview", selected_file, combined=combined)
    if not filenames:
        return HTMLResponse(templates.get_template("collections.html").render(
            **context, preview_mode=True,
        ))
    url = _archive_url(path, prefix)
    count = len(filenames)
    if page > count:
        return RedirectResponse(f"{url}?page={count}", status_code=303)
    start = (page - 1) // IMAGE_LIST_SIZE * IMAGE_LIST_SIZE
    end = min(start + IMAGE_LIST_SIZE, count)
    encoded_path = quote(path if path != "." else "", safe="/")
    return HTMLResponse(templates.get_template("preview.html").render(
        **context,
        page_number=page,
        page_count=count,
        filename=filenames[page - 1],
        folder_url=url,
        image_url=f"{image_base}/{page}/{encoded_path}",
        original_url=f"{original_base}/{page}/{encoded_path}",
        previous_url=f"{url}?page={page - 1}#preview-image" if page > 1 else None,
        next_url=f"{url}?page={page + 1}#preview-image" if page < count else None,
        image_links=[{"number": number, "name": filenames[number - 1],
                      "url": f"{url}?page={number}"} for number in range(start + 1, end + 1)],
        list_start=start + 1,
        list_end=end,
        earlier_images_url=f"{url}?page={start - IMAGE_LIST_SIZE + 1}" if start else None,
        later_images_url=f"{url}?page={end + 1}" if end < count else None,
    ))


def create_preview_app(source: Path) -> FastAPI:
    catalog = load_preview_catalog(source)
    app = _browser_app()

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/archive/", status_code=307)

    @app.get("/archive/", response_class=HTMLResponse)
    @app.get("/archive/{collection_path:path}/", response_class=HTMLResponse)
    def folder(
        collection_path: str = "", page: int = Query(default=1, ge=1), file: str | None = None
    ) -> Response:
        path = collection_path or "."
        if path not in catalog.folders:
            raise HTTPException(status_code=404, detail="Image folder not found")
        return render_preview(catalog, path, page, file)

    @app.get("/images/{page_number}/{collection_path:path}")
    def image(
        page_number: int, collection_path: str,
        size: int = Query(default=1600, ge=320, le=2400),
    ) -> Response:
        return _preview_image_response(catalog.image_path(collection_path or ".", page_number), size)

    @app.get("/original/{page_number}/{collection_path:path}")
    def original(page_number: int, collection_path: str) -> Response:
        return _original_image_response(catalog.image_path(collection_path or ".", page_number))

    return app
