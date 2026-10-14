from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
import io
from pathlib import Path, PurePosixPath
from threading import RLock
import tomllib
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlencode

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from . import storage
from .core import natural_key
from .review import effective_sections as build_effective_sections
from .review import write_reviewed_sections


if TYPE_CHECKING:
    from .preview import PreviewCatalog


WEB_DIR = Path(__file__).with_name("web")


@dataclass(frozen=True)
class ArchiveRun:
    output_dir: Path
    collection_id: str
    run_id: int
    input_dir: Path
    sections: list[dict[str, Any]]
    pages: dict[int, dict[str, Any]]
    page_sections: dict[int, dict[str, Any]]
    boundaries: dict[int, dict[str, Any]]
    original_sections: list[dict[str, Any]]
    original_boundaries: dict[int, dict[str, Any]]
    boundary_overrides: dict[str, dict[str, Any]]
    prompts: dict[str, dict[str, Any]]
    can_undo: bool
    review_warnings: tuple[str, ...] = ()

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True)
class ArchiveCollection:
    collection_id: str
    name: str
    relative_path: str
    run: ArchiveRun

    @property
    def display_path(self) -> str:
        return self.name if self.relative_path == "." else self.relative_path


@dataclass(frozen=True)
class BrowserCatalog:
    by_id: dict[str, ArchiveCollection]
    by_path: dict[str, str]
    folders: frozenset[str]
    preview: PreviewCatalog | None


def _value(value: Any) -> str:
    if isinstance(value, Undefined):
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "—"
    if value in (None, ""):
        return "—"
    return str(value)


def _recover_grouping_edits(
    snapshot: dict[str, Any], filenames: dict[int, str], recorded_at: str,
    source_orders: list[tuple[str, list[int]]],
) -> list[str]:
    def boundary_summary(boundary: dict[str, Any]) -> str:
        action = "Split before" if boundary["decision"] else "Joined before"
        return f"{action} {filenames.get(boundary['right_page_id'], 'missing page')}"

    def order_summary(order: list[int]) -> str:
        return "Saved page order: " + ", ".join(filenames.get(page_id, "missing page") for page_id in order)

    def source_order(at: str) -> list[int]:
        return next((order for completed, order in reversed(source_orders) if completed <= at), [])

    def boundaries(state: dict[str, Any]) -> dict[tuple[int, int], int]:
        return {(item["left_page_id"], item["right_page_id"]): item["decision"]
                for item in state.get("boundaries", [])}

    final_order = [item["page_id"] for item in sorted(snapshot.get("page_order", []), key=lambda item: item["position"])]
    final = {"page_order": final_order, "boundaries": snapshot.get("boundaries", []), "created_at": recorded_at}
    states = sorted(snapshot.get("undo_snapshots", []), key=lambda item: item["id"])
    if not states:
        return [boundary_summary(item) for item in final["boundaries"]] + ([order_summary(final_order)] if final_order else [])

    # Any state predating the surviving Undo stack remains a saved edit too.
    summaries = [boundary_summary(item) for item in states[0].get("boundaries", [])]
    if states[0].get("page_order"):
        summaries.append(order_summary(states[0]["page_order"]))
    for before, after in zip(states, states[1:] + [final]):
        before_order = before.get("page_order", [])
        after_order = after.get("page_order", [])
        if before_order != after_order:
            old_order = before_order or source_order(before["created_at"])
            new_order = after_order or source_order(after["created_at"])
            candidates = []
            if len(old_order) == len(new_order) and set(old_order) == set(new_order):
                first_change = next((index for index, pair in enumerate(zip(old_order, new_order)) if pair[0] != pair[1]), None)
                possible_pages = {old_order[first_change], new_order[first_change]} if first_change is not None else set()
                for page_id in sorted(possible_pages):
                    position = new_order.index(page_id)
                    if position and old_order.index(page_id) != position and (
                        [item for item in old_order if item != page_id] ==
                        [item for item in new_order if item != page_id]
                    ):
                        candidates.append((page_id, new_order[position - 1]))
            if len(candidates) == 1:
                moved, target = candidates[0]
                summaries.append(f"Moved {filenames.get(moved, 'missing page')} after {filenames.get(target, 'missing page')}")
            else:
                # Swaps can be produced by moving either page; keep the recovered order exact.
                summary = order_summary(new_order)
                old_boundaries = boundaries(before)
                adjustments = [boundary_summary({"right_page_id": right, "decision": decision})
                               for (left, right), decision in boundaries(after).items()
                               if old_boundaries.get((left, right)) != decision]
                if adjustments:
                    summary += "; " + "; ".join(adjustments)
                summaries.append(summary)
            # Boundary changes made by a move are part of that same edit.
            continue
        old_boundaries = boundaries(before)
        new_boundaries = boundaries(after)
        changes = [boundary_summary({"right_page_id": right, "decision": decision})
                   for (left, right), decision in new_boundaries.items()
                   if old_boundaries.get((left, right)) != decision]
        removed = old_boundaries.keys() - new_boundaries.keys()
        changes.extend(f"Removed boundary correction before {filenames.get(right, 'missing page')}"
                       for _left, right in sorted(removed))
        summaries.extend(changes or ["Grouping adjustment with no change to saved state"])
    return list(reversed(summaries))


def _history_rows(
    edits: list[dict[str, Any]], filenames: dict[int, str],
    source_orders: list[tuple[str, list[int]]] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for edit in edits:
        if edit["action"] != "existing_state":
            rows.append(edit)
            continue
        snapshot = edit["details"]
        summaries = _recover_grouping_edits(snapshot, filenames, edit["recorded_at"], source_orders or [])
        for label in snapshot.get("labels", []):
            filename = filenames.get(label["page_ids"][0], "missing page")
            summaries.append(f"Regenerated label for {filename}: {label['label']}")
        for flag in snapshot.get("flags", []):
            summaries.append("Flagged document starting at " + filenames.get(flag["page_id"], "missing page"))
        for summary in summaries or ["Existing saved grouping adjustments"]:
            rows.append({**edit, "summary": summary})
    return rows


def _load_collection_history(output_dir: Path, collection_id: str) -> list[dict[str, Any]]:
    """Expand imported snapshots consistently for history displays and totals."""
    edits = storage.load_edit_history(output_dir, collection_id)
    if not any(edit["action"] == "existing_state" for edit in edits):
        return edits
    with storage.connect(output_dir) as database:
        filenames = {int(row["id"]): str(row["filename"]) for row in database.execute(
            "SELECT id, filename FROM pages WHERE collection_id = ?", (collection_id,)
        )}
        source_orders = []
        for run in database.execute(
            "SELECT id, completed_at FROM analysis_runs WHERE collection_id = ? AND completed_at IS NOT NULL ORDER BY completed_at, id",
            (collection_id,),
        ):
            order = [int(row[0]) for row in database.execute(
                "SELECT page_id FROM run_pages WHERE run_id = ? ORDER BY position", (run["id"],)
            )]
            source_orders.append((str(run["completed_at"]), order))
    return _history_rows(edits, filenames, source_orders)


def _asset_version() -> str:
    project_file = WEB_DIR.parents[1] / "pyproject.toml"
    if project_file.is_file():
        with project_file.open("rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    return version("archival-organizer")


templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.filters["display_value"] = _value
templates.env.globals["asset_version"] = _asset_version()


def load_run(
    output_dir: Path,
    input_dir: Path | None = None,
    collection_id: str | None = None,
) -> ArchiveRun:
    output_dir = output_dir.resolve()
    if not (output_dir / storage.DATABASE_NAME).is_file():
        raise ValueError(f"Archive database does not exist: {output_dir / storage.DATABASE_NAME}")
    available = storage.list_collections(output_dir)
    if collection_id is None:
        if len(available) != 1:
            raise ValueError("Select a collection from this multi-collection archive")
        collection_id = str(available[0]["id"])
    data = storage.load_collection_data(output_dir, collection_id)
    collection = data["collection"]
    original_sections = data["sections"]
    if not original_sections:
        raise ValueError(f"Collection contains no sections: {collection_id}")

    configured_input = (input_dir or Path(str(collection["input_directory"]))).expanduser().resolve()
    if not configured_input.is_dir():
        raise ValueError(
            f"Image directory does not exist: {configured_input}. Pass it explicitly with --input."
        )

    pages = {int(page["page"]): page for page in data["pages"]}
    for page in pages.values():
        filename = str(page["file"])
        if Path(filename).name != filename:
            raise ValueError(f"Page {page['page']} contains an invalid filename")
        if not (configured_input / filename).is_file():
            raise ValueError(f"Source image is missing: {configured_input / filename}")
    if set(pages) != set(range(1, len(pages) + 1)):
        raise ValueError("Collection page positions must be contiguous and start at one")

    pages_by_id = {int(page["page_id"]): page for page in pages.values()}
    for section in original_sections:
        section["files"] = [
            str(pages_by_id[int(page_id)]["file"]) for page_id in section["page_ids"]
        ]
    original_boundaries = {
        int(item["right_page"]): item for item in data["original_boundaries"]
    }
    boundaries = {int(item["right_page"]): item for item in data["boundaries"]}
    sections = build_effective_sections(
        original_sections, pages, boundaries, data["reviewed_labels"]
    )
    flagged_pages = storage.load_document_flags(output_dir, collection_id)
    for section in sections:
        section["flagged"] = any(
            int(pages[number]["page_id"]) in flagged_pages
            for number in range(int(section["start_page"]), int(section["end_page"]) + 1)
        )
    page_sections = {
        page_number: section
        for section in sections
        for page_number in range(int(section["start_page"]), int(section["end_page"]) + 1)
    }
    return ArchiveRun(
        output_dir=output_dir,
        collection_id=collection_id,
        run_id=int(data["run_id"]),
        input_dir=configured_input,
        sections=sections,
        pages=pages,
        page_sections=page_sections,
        boundaries=boundaries,
        original_sections=original_sections,
        original_boundaries=original_boundaries,
        boundary_overrides=data["overrides"],
        prompts=data["prompts"],
        can_undo=bool(data["history_count"]),
        review_warnings=tuple(data["review_warnings"]),
    )


def _write_reviewed_outputs(run: ArchiveRun) -> None:
    suffix = "" if run.collection_id == "default" else f"-{run.collection_id}"
    write_reviewed_sections(run.output_dir / f"reviewed-sections{suffix}.csv", run.sections)


def _change_boundary(
    output_dir: Path,
    input_dir: Path,
    right_page: int,
    starts_new_document: bool,
    collection_id: str | None = None,
    reviewer_name: str = "John Doe",
) -> ArchiveRun:
    run = load_run(output_dir, input_dir, collection_id)
    if right_page not in run.original_boundaries:
        raise HTTPException(status_code=400, detail="Boundary does not exist")
    storage.set_boundary_override(output_dir, run.collection_id, right_page, starts_new_document, reviewer_name)
    updated = load_run(output_dir, input_dir, run.collection_id)
    _write_reviewed_outputs(updated)
    return updated


def _undo_boundary_change(
    output_dir: Path, input_dir: Path, collection_id: str | None = None, reviewer_name: str = "John Doe",
) -> ArchiveRun:
    run = load_run(output_dir, input_dir, collection_id)
    if not storage.undo_boundary_override(output_dir, run.collection_id, reviewer_name):
        return run
    updated = load_run(output_dir, input_dir, run.collection_id)
    _write_reviewed_outputs(updated)
    return updated


def _move_page(
    output_dir: Path,
    input_dir: Path,
    page_number: int,
    target_page: int,
    collection_id: str | None = None,
    reviewer_name: str = "John Doe",
) -> ArchiveRun:
    run = load_run(output_dir, input_dir, collection_id)
    if page_number not in run.pages or target_page not in run.pages:
        raise HTTPException(status_code=400, detail="Page number does not exist")
    try:
        storage.move_page_after(output_dir, run.collection_id, page_number, target_page, reviewer_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = load_run(output_dir, input_dir, run.collection_id)
    _write_reviewed_outputs(updated)
    return updated


@lru_cache(maxsize=24)
def _preview(path_string: str, modified_ns: int, max_size: int) -> bytes:
    del modified_ns
    with Image.open(path_string) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=84, optimize=True)
        return buffer.getvalue()


def _page_context(
    run: ArchiveRun,
    page_number: int,
    base_url: str,
    collection_path: str | None,
    content_url: str | None = None,
) -> dict[str, Any]:
    content_url = base_url if content_url is None else content_url
    page = run.pages[page_number]
    section = run.page_sections[page_number]
    section_start = int(section["start_page"])
    section_end = int(section["end_page"])
    previous_page = page_number - 1 if page_number > 1 else None
    next_page = page_number + 1 if page_number < run.page_count else None
    metadata = [
        ("Title", page.get("title")),
        ("Summary", page.get("summary")),
        ("Document type", page.get("document_type")),
        ("Page role", page.get("page_role")),
        ("Date", page.get("date")),
        ("Language", page.get("language")),
        ("Printed page", page.get("printed_page_number")),
        ("Total pages", page.get("printed_total_pages")),
        ("Author", page.get("author")),
        ("Sender", page.get("sender")),
        ("Recipient", page.get("recipient")),
        ("Organization", page.get("organization")),
        ("Reference", page.get("reference_number")),
        ("Section heading", page.get("section_heading")),
        ("Places", page.get("places")),
        ("Subjects", page.get("subjects")),
        ("Starts mid-sentence", page.get("starts_mid_sentence")),
        ("Ends mid-sentence", page.get("ends_mid_sentence")),
        ("Continuation marker", page.get("continuation_marker")),
        ("Possible attachment", page.get("possible_attachment")),
        ("Header", page.get("header_text")),
        ("Footer", page.get("footer_text")),
    ]
    review_actions: list[dict[str, Any]] = [
        {
            "url": f"{content_url}/review/boundaries/{section_start}/join",
            "label": "Attach to previous",
            "row": "attach",
            "secondary": False,
            "disabled": section_start == 1,
        },
        {
            "url": f"{content_url}/review/boundaries/{page_number}/split",
            "label": "Split before this page",
            "row": "split",
            "secondary": False,
            "disabled": page_number == section_start,
        },
        {
            "url": f"{content_url}/review/boundaries/{section_end + 1}/join",
            "label": "Attach to next",
            "row": "attach",
            "secondary": False,
            "disabled": section_end == run.page_count,
        },
    ]
    boundary = run.boundaries.get(page_number)
    boundary_view = None
    if boundary:
        boundary_view = {
            "decision": "Starts this section"
            if boundary.get("starts_new_document")
            else "Continues previous page",
            "confidence_label": f'{float(boundary.get("confidence", 0)):.0%}',
            "reason": boundary.get("reason"),
        }
    return {
        "run": run,
        "page": page,
        "page_number": page_number,
        "section": section,
        "section_number": int(section["section"]),
        "section_pages": range(section_start, section_end + 1),
        "base_url": base_url,
        "collection_path": collection_path,
        "previous_url": f"{base_url}/?page={previous_page}#page-strip" if previous_page else None,
        "next_url": f"{base_url}/?page={next_page}#page-strip" if next_page else None,
        "confidence_label": f'{float(page.get("extraction_confidence", 0)):.0%}',
        "metadata": [{"label": label, "value": value} for label, value in metadata],
        "review_actions": review_actions,
        "flag_url": f"{content_url}/review/pages/{page_number}/{'unflag' if section['flagged'] else 'flag'}",
        "undo_url": f"{content_url}/review/undo?page={page_number}",
        "history_url": f"{content_url}/history",
        "move_url": f"{content_url}/review/pages/{page_number}/move",
        "image_url": f"{content_url}/images/{page_number}",
        "original_url": (
            f"/original/{page_number}/{quote(collection_path if collection_path != '.' else '', safe='/')}"
            if collection_path is not None else f"{content_url}/images/{page_number}/original"
        ),
        "boundary": boundary_view,
    }


def _render_page(
    run: ArchiveRun,
    page_number: int,
    *,
    base_url: str = "",
    collection_path: str | None = None,
    content_url: str | None = None,
    folder_context: dict[str, Any] | None = None,
) -> str:
    return templates.get_template("page.html").render(
        **_page_context(run, page_number, base_url, collection_path, content_url),
        **(folder_context or {}),
    )


def load_collections(
    output_dir: Path, input_dir: Path | None = None, *, allow_empty: bool = False
) -> list[ArchiveCollection]:
    output_dir = output_dir.resolve()
    items = storage.list_collections(output_dir)
    if not items and not allow_empty:
        raise ValueError(f"Archive contains no completed collections: {output_dir}")
    input_root = input_dir.expanduser().resolve() if input_dir else None
    collections: list[ArchiveCollection] = []
    for item in items:
        collection_id = str(item["id"])
        relative_path = str(item["relative_path"])
        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != relative_path:
            raise ValueError(f"Invalid collection path: {relative_path}")
        collection_input = None
        if input_root is not None:
            collection_input = input_root if relative_path == "." else (input_root / relative_path).resolve()
            if not collection_input.is_relative_to(input_root):
                raise ValueError(f"Collection input escapes the selected directory: {relative_path}")
        collections.append(ArchiveCollection(
            collection_id=collection_id,
            name=str(item["name"]),
            relative_path=relative_path,
            run=load_run(output_dir, collection_input, collection_id),
        ))
    return collections


def _archive_url(relative_path: str, prefix: str = "/archive") -> str:
    return f"{prefix}/" if relative_path == "." else f"{prefix}/{quote(relative_path, safe='/')}/"


def _browser_controls(
    path: str, mode: str, filename: str | None = None, *, combined: bool = False
) -> dict[str, Any]:
    query = "?" + urlencode({"file": filename}) if filename is not None else ""
    return {
        "mode": mode,
        "preview_url": _archive_url(path, "/preview") + query if combined else None,
        "analyzed_url": _archive_url(path) + query if combined else None,
    }


def _folder_context(
    collections: list[ArchiveCollection], relative_path: str = "."
) -> dict[str, Any]:
    """Derive navigation from completed collections, including image-free ancestors."""
    return _navigation_context(
        {collection.relative_path: collection.run.page_count for collection in collections},
        relative_path,
        edit_counts={
            collection.relative_path: len(_load_collection_history(collection.run.output_dir, collection.collection_id))
            for collection in collections
        },
    )


def _navigation_context(
    page_counts: Mapping[str, int], relative_path: str = ".", prefix: str = "/archive",
    *, edit_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build the same folder navigation for database and filesystem catalogs."""
    parent = PurePosixPath(relative_path)
    crumbs = [{"label": "Archive Top", "url": _archive_url(".", prefix)}]
    for index, part in enumerate(parent.parts, start=1):
        crumbs.append({"label": part, "url": _archive_url("/".join(parent.parts[:index]), prefix)})
    children: dict[str, dict[str, Any]] = {}
    for collection_path, page_count in page_counts.items():
        path = PurePosixPath(collection_path)
        if path == parent or not path.is_relative_to(parent):
            continue
        name = path.relative_to(parent).parts[0]
        child = children.setdefault(name, {
            "name": name,
            "url": _archive_url((parent / name).as_posix(), prefix),
            "page_count": 0,
            "collection_count": 0,
        })
        child["page_count"] += page_count
        child["collection_count"] += 1
        if edit_counts is not None:
            child["edit_count"] = child.get("edit_count", 0) + edit_counts.get(collection_path, 0)
    return {
        "breadcrumbs": crumbs,
        "folder_title": parent.name if parent.parts else "Archive Top",
        "children": sorted(children.values(), key=lambda item: natural_key(Path(item["name"]))),
    }


def _render_collections(collections: list[ArchiveCollection], relative_path: str = ".") -> str:
    return templates.get_template("collections.html").render(
        **_folder_context(collections, relative_path)
    )


def _image_response(run: ArchiveRun, page_number: int, size: int) -> Response:
    page = run.pages.get(page_number)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    path = run.input_dir / str(page["file"])
    return _preview_image_response(path, size)


def _preview_image_response(path: Path, size: int) -> Response:
    try:
        content = _preview(str(path), path.stat().st_mtime_ns, size)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Image could not be read") from exc
    return Response(content, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})


def _original_response(run: ArchiveRun, page_number: int) -> FileResponse:
    page = run.pages.get(page_number)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    path = run.input_dir / str(page["file"])
    return _original_image_response(path)


def _original_image_response(path: Path) -> FileResponse:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    media_types = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp",
    }
    return FileResponse(
        path,
        media_type=media_types.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}"},
    )


def _browser_app() -> FastAPI:
    app = FastAPI(title="Archival organizer browser", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> FileResponse:
        return FileResponse(WEB_DIR / "static" / "robots.txt", media_type="text/plain")

    return app


def create_app(output_dir: Path, input_dir: Path | None = None) -> FastAPI:
    app = _browser_app()
    if not (output_dir / storage.DATABASE_NAME).is_file():
        raise ValueError(f"Archive database does not exist: {output_dir / storage.DATABASE_NAME}")

    storage.initialize(output_dir)
    from .preview import load_preview_catalog, render_preview

    lock = RLock()

    def load_catalog() -> BrowserCatalog:
        collections = load_collections(output_dir, input_dir, allow_empty=input_dir is not None)
        by_id = {collection.collection_id: collection for collection in collections}
        by_path = {collection.relative_path: collection.collection_id for collection in collections}
        if len(by_path) != len(collections):
            raise ValueError("Archive contains duplicate collection paths")
        folders = {"."}
        for path in by_path:
            folders.add(path)
            folders.update(parent.as_posix() for parent in PurePosixPath(path).parents)
        preview = load_preview_catalog(input_dir, allow_empty=True) if input_dir is not None else None
        return BrowserCatalog(by_id, by_path, frozenset(folders), preview)

    state = load_catalog()

    def selected(collection_id: str) -> ArchiveCollection:
        collection = state.by_id.get(collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        return collection

    def remember(collection: ArchiveCollection, run: ArchiveRun) -> ArchiveCollection:
        updated = ArchiveCollection(
            collection.collection_id, collection.name, collection.relative_path, run
        )
        state.by_id[collection.collection_id] = updated
        return updated

    def page_redirect(collection: ArchiveCollection, page: int) -> RedirectResponse:
        return RedirectResponse(
            url=f"{_archive_url(collection.relative_path)}?page={min(page, collection.run.page_count)}",
            status_code=303,
        )

    def update_collection(
        collection_id: str, right_page: int | None, decision: bool | None, reviewer_name: str,
    ) -> ArchiveCollection:
        with lock:
            collection = selected(collection_id)
            if right_page is None:
                run = _undo_boundary_change(
                    collection.run.output_dir, collection.run.input_dir, collection_id, reviewer_name
                )
            else:
                assert decision is not None
                run = _change_boundary(
                    collection.run.output_dir,
                    collection.run.input_dir,
                    right_page,
                    decision,
                    collection_id,
                    reviewer_name,
                )
            return remember(collection, run)

    @app.get("/")
    def collection_index() -> RedirectResponse:
        return RedirectResponse("/archive/", status_code=307)

    def known_folder(snapshot: BrowserCatalog, path: str) -> bool:
        return path in snapshot.folders or (snapshot.preview is not None and path in snapshot.preview.folders)

    @app.get("/archive/", response_class=HTMLResponse)
    @app.get("/archive/{collection_path:path}/", response_class=HTMLResponse)
    def archive_folder(
        collection_path: str = "", page: int = Query(default=1, ge=1), file: str | None = None
    ) -> Response:
        path = collection_path or "."
        snapshot = state
        if not known_folder(snapshot, path):
            raise HTTPException(status_code=404, detail="Archive folder not found")
        current = list(snapshot.by_id.values())
        context = _folder_context(current, path)
        collection_id = snapshot.by_path.get(path)
        collection = snapshot.by_id.get(collection_id) if collection_id is not None else None
        notice = None
        if collection is not None and file is not None:
            match = next((number for number, item in collection.run.pages.items() if item["file"] == file), None)
            if match is None:
                notice = "This image has not been analyzed in the latest completed run."
            else:
                page = match
        filename = file
        if collection is not None and notice is None:
            if page not in collection.run.pages:
                return page_redirect(collection, page)
            filename = str(collection.run.pages[page]["file"])
        context["browser_controls"] = _browser_controls(
            path, "analyzed", filename, combined=snapshot.preview is not None,
        )
        if collection is None or notice is not None:
            if collection is None and (file is not None or not context["children"]):
                notice = "No completed analysis for this folder yet."
            return HTMLResponse(templates.get_template("collections.html").render(
                **context, notice=notice,
                analyzed_pages_url=_archive_url(path) if collection is not None else None,
            ))
        return HTMLResponse(_render_page(
            collection.run, page,
            base_url=_archive_url(path).rstrip("/"),
            content_url=f"/collections/{quote(collection.collection_id, safe='')}",
            collection_path=path,
            folder_context=context,
        ))

    if input_dir is not None:
        @app.get("/preview/", response_class=HTMLResponse)
        @app.get("/preview/{collection_path:path}/", response_class=HTMLResponse)
        def preview_folder(
            collection_path: str = "", page: int = Query(default=1, ge=1), file: str | None = None
        ) -> Response:
            path = collection_path or "."
            snapshot = state
            if not known_folder(snapshot, path):
                raise HTTPException(status_code=404, detail="Image folder not found")
            assert snapshot.preview is not None
            return render_preview(snapshot.preview, path, page, file, combined=True)

        @app.get("/preview-images/{page_number}/{collection_path:path}")
        def preview_image(
            page_number: int, collection_path: str,
            size: int = Query(default=1600, ge=320, le=2400),
        ) -> Response:
            assert state.preview is not None
            return _preview_image_response(state.preview.image_path(collection_path or ".", page_number), size)

        @app.get("/preview-original/{page_number}/{collection_path:path}")
        def preview_original(page_number: int, collection_path: str) -> Response:
            assert state.preview is not None
            return _original_image_response(state.preview.image_path(collection_path or ".", page_number))

    @app.get("/collections/{collection_id}/")
    def collection_page(collection_id: str, page: int = Query(default=1, ge=1)) -> RedirectResponse:
        return page_redirect(selected(collection_id), page)

    @app.get("/original/{page_number}/{collection_path:path}")
    def original_image(page_number: int, collection_path: str) -> FileResponse:
        collection_id = state.by_path.get(collection_path or ".")
        if collection_id is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        return _original_response(selected(collection_id).run, page_number)

    @app.get("/collections/{collection_id}/images/{page_number}")
    def collection_image_preview(
        collection_id: str,
        page_number: int,
        size: int = Query(default=1600, ge=320, le=2400),
    ) -> Response:
        return _image_response(selected(collection_id).run, page_number, size)

    @app.get("/collections/{collection_id}/images/{page_number}/original")
    def collection_original_image(collection_id: str, page_number: int) -> FileResponse:
        return _original_response(selected(collection_id).run, page_number)

    async def require_reviewer(request: Request) -> str:
        values = parse_qs((await request.body()).decode("utf-8"))
        name = values.get("reviewer_name", [request.headers.get("X-Reviewer-Name", "")])[0].strip()
        if not name or len(name) > 100:
            raise HTTPException(status_code=400, detail="Enter a reviewer name (1–100 characters) before making edits")
        return name

    @app.get("/collections/{collection_id}/history", response_class=HTMLResponse)
    def collection_history(collection_id: str) -> str:
        collection = selected(collection_id)
        return templates.get_template("history.html").render(
            collection=collection, back_url=_archive_url(collection.relative_path),
            edits=_load_collection_history(collection.run.output_dir, collection_id),
        )

    @app.post("/collections/{collection_id}/review/boundaries/{right_page}/split")
    def collection_split(collection_id: str, right_page: int, reviewer_name: str = Depends(require_reviewer)) -> RedirectResponse:
        return page_redirect(update_collection(collection_id, right_page, True, reviewer_name), right_page)

    @app.post("/collections/{collection_id}/review/boundaries/{right_page}/join")
    def collection_join(collection_id: str, right_page: int, reviewer_name: str = Depends(require_reviewer)) -> RedirectResponse:
        return page_redirect(update_collection(collection_id, right_page, False, reviewer_name), right_page)

    def flag_collection_document(collection_id: str, page_number: int, flagged: bool, reviewer_name: str) -> RedirectResponse:
        with lock:
            collection = selected(collection_id)
            section = collection.run.page_sections.get(page_number)
            if section is None:
                raise HTTPException(status_code=404, detail="Page not found")
            page_ids = [
                int(collection.run.pages[number]["page_id"])
                for number in range(int(section["start_page"]), int(section["end_page"]) + 1)
            ]
            storage.set_document_flag(collection.run.output_dir, collection_id, page_ids, flagged, reviewer_name)
            run = load_run(collection.run.output_dir, collection.run.input_dir, collection_id)
            return page_redirect(remember(collection, run), page_number)

    @app.post("/collections/{collection_id}/review/pages/{page_number}/flag")
    def collection_flag(collection_id: str, page_number: int, reviewer_name: str = Depends(require_reviewer)) -> RedirectResponse:
        return flag_collection_document(collection_id, page_number, True, reviewer_name)

    @app.post("/collections/{collection_id}/review/pages/{page_number}/unflag")
    def collection_unflag(collection_id: str, page_number: int, reviewer_name: str = Depends(require_reviewer)) -> RedirectResponse:
        return flag_collection_document(collection_id, page_number, False, reviewer_name)

    def move_collection_page(collection_id: str, page_number: int, target_page: int, reviewer_name: str) -> RedirectResponse:
        with lock:
            collection = selected(collection_id)
            moving_page_id = int(collection.run.pages.get(page_number, {}).get("page_id", 0))
            run = _move_page(
                collection.run.output_dir,
                collection.run.input_dir,
                page_number,
                target_page,
                collection_id,
                reviewer_name,
            )
            updated = remember(collection, run)
            new_position = next(
                number
                for number, page in run.pages.items()
                if int(page["page_id"]) == moving_page_id
            )
            return page_redirect(updated, new_position)

    @app.post("/collections/{collection_id}/review/pages/{page_number}/move")
    async def collection_move_page(
        collection_id: str, page_number: int, request: Request, reviewer_name: str = Depends(require_reviewer),
    ) -> RedirectResponse:
        values = parse_qs((await request.body()).decode("utf-8"))
        try:
            target_page = int(values.get("target_page", [""])[0])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Enter a valid page number") from exc
        return await run_in_threadpool(move_collection_page, collection_id, page_number, target_page, reviewer_name)

    @app.post("/collections/{collection_id}/review/undo")
    def collection_undo(
        collection_id: str, page: int = Query(default=1, ge=1), reviewer_name: str = Depends(require_reviewer),
    ) -> RedirectResponse:
        collection = update_collection(collection_id, None, None, reviewer_name)
        return page_redirect(collection, page)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Browse an analyzed archive or preview source images.")
    parser.add_argument(
        "directory", type=Path,
        help="Analysis output containing archive.sqlite3, or source image directory with --preview",
    )
    parser.add_argument(
        "--preview", action="store_true", help="Browse source images without analysis or a database"
    )
    parser.add_argument(
        "--input", type=Path, help="Source image root: enables Preview / Analyzed switching and overrides stored image locations"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    args = parser.parse_args()
    if args.preview and args.input is not None:
        parser.error("--preview uses the positional source directory; do not combine it with --input")
    try:
        if args.preview:
            from .preview import create_preview_app

            app = create_preview_app(args.directory)
        else:
            app = create_app(args.directory, args.input)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
