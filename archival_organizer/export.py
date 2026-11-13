"""Portable JSONL exports from a private, consistent SQLite snapshot."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from importlib.resources import files
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
import time
from tempfile import TemporaryDirectory
from typing import Any, Iterable

from . import storage
from .migrations import BASE_SCHEMA_VERSION, SCHEMA_VERSION
from .review import effective_sections


EXPORT_VERSION = 1


def _silent(message: str) -> None:
    pass


def _relative_path(*parts: str) -> str:
    for part in parts:
        if (PurePosixPath(part).is_absolute() or PureWindowsPath(part).drive
                or "\\" in part or ".." in PurePosixPath(part).parts):
            raise ValueError(f"Cannot export unsafe relative image path: {part!r}")
    return PurePosixPath(*parts).as_posix()


def _write_jsonl(
    path: Path, rows: Iterable[dict[str, Any]], report: Callable[[str], None] = _silent,
) -> int:
    count = 0
    last_report = time.monotonic()
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            count += 1
            if count % 1000 == 0 or time.monotonic() - last_report >= 2:
                handle.flush()
                report(f"[export] {path.name}: {count:,} records, {handle.tell() / 1024**2:.2f} MiB written")
                last_report = time.monotonic()
    report(f"[export] {path.name}: complete — {count:,} records, {path.stat().st_size / 1024**2:.2f} MiB")
    return count


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _history(
    database: sqlite3.Connection, directory: Path, report: Callable[[str], None] = _silent,
) -> tuple[dict, dict]:
    directory.mkdir()
    counts, schemas = {}, {}
    tables = database.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for index, table_row in enumerate(tables, start=1):
        table = table_row[0]
        report(f"[export] History table {index}/{len(tables)}: {table}")
        # Only ordinary table identifiers may become filenames.
        if not table.replace("_", "").isalnum():
            raise ValueError(f"Unsupported table name: {table!r}")
        columns = [dict(row) for row in database.execute(f"PRAGMA table_info({_quote(table)})")]
        schemas[table] = {
            "columns": columns,
            "foreign_keys": [dict(row) for row in database.execute(
                f"PRAGMA foreign_key_list({_quote(table)})"
            )],
        }
        order = [column["name"] for column in sorted(columns, key=lambda c: c["pk"]) if column["pk"]]
        order = order or [column["name"] for column in columns]
        query = f"SELECT * FROM {_quote(table)} ORDER BY " + ", ".join(map(_quote, order))

        def records():
            for row in database.execute(query):
                record = dict(row)
                if table == "collections":
                    record["relative_path"] = _relative_path(record["relative_path"])
                    record["input_directory"] = record["relative_path"]
                if table == "pages":
                    _relative_path(record["filename"])
                yield record

        counts[f"history/{table}.jsonl"] = _write_jsonl(directory / f"{table}.jsonl", records(), report)
    return counts, schemas


def _current(
    snapshot: Path, directory: Path, report: Callable[[str], None] = _silent,
) -> dict[str, int]:
    counts = {f"{name}.jsonl": 0 for name in ("collections", "pages", "documents")}
    with ExitStack() as stack:
        handles = {name: stack.enter_context((directory / name).open("w", encoding="utf-8", newline="\n"))
                   for name in counts}

        last_report = time.monotonic()

        def progress():
            for handle in handles.values():
                handle.flush()
            size = sum(handle.tell() for handle in handles.values())
            report(f"[export] Reviewed view: {counts['collections.jsonl']:,} collections, "
                   f"{counts['pages.jsonl']:,} pages, {counts['documents.jsonl']:,} documents; "
                   f"{size / 1024**2:.2f} MiB written")

        def write(name, record):
            nonlocal last_report
            filename = f"{name}.jsonl"
            handles[filename].write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            counts[filename] += 1
            if sum(counts.values()) % 1000 == 0 or time.monotonic() - last_report >= 2:
                progress()
                last_report = time.monotonic()

        with storage.connect(snapshot) as database:
            collections = [dict(row) for row in database.execute("SELECT * FROM collections ORDER BY id")]
            latest = {row["collection_id"]: row["run_id"] for row in database.execute(
                "SELECT collection_id, MAX(id) AS run_id FROM analysis_runs "
                "WHERE completed_at IS NOT NULL GROUP BY collection_id"
            )}
        completions = storage.load_collection_completions(snapshot)
        for index, collection in enumerate(collections, start=1):
            collection_id = collection["id"]
            relative = _relative_path(collection["relative_path"])
            report(f"[export] Reviewed collection {index}/{len(collections)}: {relative}")
            collection.update(relative_path=relative, input_directory=relative)
            run_id = latest.get(collection_id)
            data = storage.load_collection_data(snapshot, collection_id) if run_id is not None else None
            write("collections", dict(collection, latest_completed_run_id=run_id,
                                      completion=completions.get(collection_id),
                                      review_warnings=data["review_warnings"] if data else []))
            if data is None:
                progress()
                continue
            pages = {page["page"]: page for page in data["pages"]}
            boundaries = {item["right_page"]: item for item in data["boundaries"]}
            sections = effective_sections(data["sections"], pages, boundaries, data["reviewed_labels"])
            flags = storage.load_document_flags(snapshot, collection_id)
            for section in sections:
                # Reviewed groups have no persisted document identity: use an explicit snapshot ID.
                document_id = f"{collection_id}:{run_id}:{section['section']}"
                record = dict(section)
                record["page_ids"] = [pages[position]["page_id"] for position in
                                      range(section["start_page"], section["end_page"] + 1)]
                record["original_document_id"] = record.pop("document_id", None)
                record.update(id=document_id, collection_id=collection_id, run_id=run_id,
                              flagged=bool(flags.intersection(record["page_ids"])))
                record["label_review"] = bool(record["label_needs_review"] or record["label_confidence"] < 0.75)
                record["grouping_review"] = record["lowest_boundary_confidence"] < 0.75
                record["image_paths"] = [_relative_path(relative, name) for name in record.pop("files")]
                write("documents", record)
                for position in range(section["start_page"], section["end_page"] + 1):
                    page = pages[position]
                    write("pages", {
                        "page_id": page["page_id"], "collection_id": collection_id,
                        "run_id": run_id, "document_id": document_id, "position": position,
                        "image_path": _relative_path(relative, page["file"]),
                        "analysis": {key: value for key, value in page.items()
                                     if key not in {"page_id", "page", "file"}},
                        "boundary_before": boundaries.get(position),
                    })
            progress()
    return counts


def export_archive(
    output: Path, destination: Path, *, report: Callable[[str], None] = _silent,
) -> Path:
    """Export all stored records and the current reviewed view, without changing the archive."""
    source = output.resolve() / storage.DATABASE_NAME
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Export destination already exists: {destination}")
    if not source.is_file():
        raise ValueError(f"Archive database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".archive-export-", dir=destination.parent) as temporary:
        working = Path(temporary)
        snapshot = working / "snapshot"
        snapshot.mkdir()
        snapshot_file = snapshot / storage.DATABASE_NAME
        report("[export] Creating consistent database snapshot...")
        last_snapshot_report = time.monotonic()

        def snapshot_progress(status, remaining, total):
            nonlocal last_snapshot_report
            if remaining == 0 or time.monotonic() - last_snapshot_report >= 2:
                percent = 100 * (total - remaining) / total if total else 100
                report(f"[export] Database snapshot: {percent:.0f}%")
                last_snapshot_report = time.monotonic()

        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(snapshot_file)) as copy:
                original.backup(copy, pages=1024, progress=snapshot_progress)
        with closing(sqlite3.connect(snapshot_file)) as database:
            database.row_factory = sqlite3.Row
            try:
                version = int(database.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0])
            except (sqlite3.Error, TypeError, ValueError) as exc:
                raise ValueError("Archive database has no valid schema version") from exc
            if not BASE_SCHEMA_VERSION <= version <= SCHEMA_VERSION:
                raise ValueError(f"Unsupported database schema {version}; supported versions are "
                                 f"{BASE_SCHEMA_VERSION}–{SCHEMA_VERSION}")
            bundle = working / "bundle"
            bundle.mkdir()
            counts, schemas = _history(database, bundle / "history", report)
        # Upgrade only the disposable snapshot to reuse the application's review logic.
        report("[export] Preparing current reviewed view...")
        storage.initialize(snapshot)
        counts.update(_current(snapshot, bundle, report))
        manifest = {
            "format": "archival-organizer", "export_version": EXPORT_VERSION,
            "source_schema_version": version, "current_view_schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "image_paths_relative_to": "analysis_input_root", "images_included": False,
            "record_counts": counts, "history_schema": schemas,
            "history_encoding": "Original columns and SQLite values; *_json columns remain JSON strings.",
            "path_normalization": "collections.input_directory is replaced by collections.relative_path.",
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (bundle / "README.md").write_text(
            files("archival_organizer").joinpath("export_readme.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        total_bytes = sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file())
        report("[export] Publishing completed export...")
        # Reserve the destination exclusively, including against concurrent exporters.
        destination.mkdir()
        try:
            bundle.replace(destination)
        except BaseException:
            destination.rmdir()
            raise
    report(f"[export] Complete: {sum(counts.values()):,} JSONL records, "
           f"{total_bytes / 1024**2:.2f} MiB in {destination}")
    return destination
