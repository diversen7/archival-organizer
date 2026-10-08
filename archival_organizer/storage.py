from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from . import migrations
from .migrations import SCHEMA_VERSION


DATABASE_NAME = "archive.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(output_dir: Path) -> Iterator[sqlite3.Connection]:
    output_dir.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(output_dir / DATABASE_NAME)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA journal_mode = WAL")
    try:
        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def initialize(output_dir: Path) -> None:
    """Create an archive or atomically apply all supported schema upgrades."""
    with connect(output_dir) as database:
        # Serialize initializers and include DDL in rollback on any failure.
        database.execute("BEGIN IMMEDIATE")
        tables = {
            row["name"] for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            for statement in migrations.BASE_SCHEMA:
                database.execute(statement)
            version = migrations.BASE_SCHEMA_VERSION
            database.execute(
                "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(version),),
            )
        else:
            existing = None
            if "metadata" in tables:
                existing = database.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
            try:
                version = int(existing["value"]) if existing is not None else None
            except (ValueError, TypeError):
                version = None
            if version is None:
                raise ValueError("Archive database has no valid schema version; left unchanged.")
            if version > SCHEMA_VERSION:
                raise ValueError(
                    f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}. "
                    "Upgrade the application to open this archive."
                )
            if version < migrations.BASE_SCHEMA_VERSION:
                raise ValueError(
                    f"Unsupported database schema {version}; migrations start at "
                    f"version {migrations.BASE_SCHEMA_VERSION}. Use a fresh output directory."
                )

        while version < SCHEMA_VERSION:
            statements = migrations.MIGRATIONS.get(version)
            if statements is None:
                raise ValueError(f"Missing database migration from schema {version}.")
            for statement in statements:
                database.execute(statement)
            version += 1
            database.execute(
                "UPDATE metadata SET value = ? WHERE key = 'schema_version'", (str(version),)
            )


def load_boundary_batch(output_dir: Path, cache_key: str) -> list[dict[str, Any]] | None:
    with connect(output_dir) as database:
        row = database.execute(
            "SELECT result_json FROM boundary_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
    return None if row is None else json.loads(row["result_json"])


def save_boundary_batch(
    output_dir: Path, cache_key: str, decisions: list[dict[str, Any]]
) -> None:
    """Commit a model response independently of whether its run finishes."""
    with connect(output_dir) as database:
        database.execute(
            """INSERT INTO boundary_cache(cache_key, result_json, created_at) VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                result_json = excluded.result_json, created_at = excluded.created_at""",
            (cache_key, json.dumps(decisions, ensure_ascii=False), _now()),
        )


def upsert_collection(
    output_dir: Path,
    collection_id: str,
    name: str,
    relative_path: str,
    input_directory: Path,
) -> None:
    now = _now()
    with connect(output_dir) as database:
        database.execute(
            """
            INSERT INTO collections(id, name, relative_path, input_directory, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                relative_path = excluded.relative_path,
                input_directory = excluded.input_directory,
                updated_at = excluded.updated_at
            WHERE name != excluded.name OR relative_path != excluded.relative_path
                OR input_directory != excluded.input_directory
            """,
            (collection_id, name, relative_path, str(input_directory.resolve()), now, now),
        )


def sync_pages(output_dir: Path, collection_id: str, paths: list[Path]) -> None:
    """Register source files and replace only their mutable collection order."""
    with connect(output_dir) as database:
        page_ids: list[int] = []
        for path in paths:
            database.execute(
                """INSERT OR IGNORE INTO pages(collection_id, filename, created_at)
                VALUES (?, ?, ?)""",
                (collection_id, path.name, _now()),
            )
            row = database.execute(
                "SELECT id FROM pages WHERE collection_id = ? AND filename = ?",
                (collection_id, path.name),
            ).fetchone()
            assert row is not None
            page_ids.append(int(row["id"]))
        database.execute("DELETE FROM page_order WHERE collection_id = ?", (collection_id,))
        database.executemany(
            "INSERT INTO page_order(collection_id, page_id, position) VALUES (?, ?, ?)",
            [
                (collection_id, page_id, position)
                for position, page_id in enumerate(page_ids, start=1)
            ],
        )


def start_run(
    output_dir: Path,
    collection_id: str,
    model: str,
    page_analysis_key: str,
    prompts: dict[str, str],
) -> int:
    with connect(output_dir) as database:
        cursor = database.execute(
            """INSERT INTO analysis_runs(
                collection_id, model, page_analysis_key, started_at
            ) VALUES (?, ?, ?, ?)""",
            (collection_id, model, page_analysis_key, _now()),
        )
        run_id = int(cursor.lastrowid)
        for kind, content in prompts.items():
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            database.execute(
                """INSERT OR IGNORE INTO prompt_versions(
                    kind, content, content_hash, created_at
                ) VALUES (?, ?, ?, ?)""",
                (kind, content, content_hash, _now()),
            )
            prompt = database.execute(
                "SELECT id FROM prompt_versions WHERE kind = ? AND content_hash = ?",
                (kind, content_hash),
            ).fetchone()
            assert prompt is not None
            database.execute(
                """INSERT INTO run_prompts(run_id, kind, prompt_version_id)
                VALUES (?, ?, ?)""",
                (run_id, kind, int(prompt["id"])),
            )
        return run_id


def load_cached_page(
    output_dir: Path,
    collection_id: str,
    position: int,
    filename: str,
    analysis_key: str,
) -> tuple[dict[str, Any], int] | None:
    with connect(output_dir) as database:
        row = database.execute(
            """
            SELECT pa.id, pa.result_json
            FROM page_order po
            JOIN pages p ON p.id = po.page_id
            JOIN page_analyses pa ON pa.page_id = p.id
            WHERE po.collection_id = ? AND po.position = ? AND p.filename = ?
                AND pa.analysis_key = ?
            ORDER BY pa.id DESC LIMIT 1
            """,
            (collection_id, position, filename, analysis_key),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["result_json"]), int(row["id"])


def attach_cached_page(
    output_dir: Path, run_id: int, position: int, analysis_id: int
) -> None:
    with connect(output_dir) as database:
        row = database.execute(
            "SELECT page_id FROM page_analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Page analysis does not exist: {analysis_id}")
        database.execute(
            """INSERT INTO run_pages(run_id, page_id, analysis_id, position)
            VALUES (?, ?, ?, ?)""",
            (run_id, int(row["page_id"]), analysis_id, position),
        )


def save_page_analysis(
    output_dir: Path,
    collection_id: str,
    run_id: int,
    page: dict[str, Any],
    model: str,
    analysis_key: str,
    *,
    source_hash: str | None = None,
) -> int:
    position = int(page["page"])
    filename = str(page["file"])
    with connect(output_dir) as database:
        row = database.execute(
            """SELECT p.id FROM page_order po JOIN pages p ON p.id = po.page_id
            WHERE po.collection_id = ? AND po.position = ? AND p.filename = ?""",
            (collection_id, position, filename),
        ).fetchone()
        if row is None:
            raise ValueError(f"Page is not registered at position {position}: {filename}")
        page_id = int(row["id"])
        cursor = database.execute(
            """INSERT INTO page_analyses(
                page_id, run_id, model, analysis_key, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                page_id,
                run_id,
                model,
                analysis_key,
                json.dumps(page, ensure_ascii=False),
                _now(),
            ),
        )
        analysis_id = int(cursor.lastrowid)
        if source_hash is not None:
            database.execute(
                "INSERT INTO analysis_sources(analysis_id, sha256) VALUES (?, ?)",
                (analysis_id, source_hash),
            )
        database.execute(
            """INSERT INTO run_pages(run_id, page_id, analysis_id, position)
            VALUES (?, ?, ?, ?)""",
            (run_id, page_id, analysis_id, position),
        )
        return analysis_id


def save_results(
    output_dir: Path,
    collection_id: str,
    sections: list[dict[str, Any]],
    boundaries: dict[int, dict[str, Any]],
    run_id: int,
) -> None:
    with connect(output_dir) as database:
        run_pages = {
            int(row["position"]): int(row["page_id"])
            for row in database.execute(
                "SELECT position, page_id FROM run_pages WHERE run_id = ?", (run_id,)
            )
        }
        for right_position, item in sorted(boundaries.items()):
            database.execute(
                """INSERT INTO boundaries(
                    run_id, left_page_id, right_page_id, right_position,
                    starts_new_document, confidence, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    run_pages[right_position - 1],
                    run_pages[right_position],
                    right_position,
                    int(bool(item["starts_new_document"])),
                    float(item["confidence"]),
                    str(item["reason"]),
                ),
            )
        for section in sections:
            cursor = database.execute(
                """INSERT INTO documents(
                    run_id, document_number, label, document_type, summary, date,
                    places_json, subjects_json, label_confidence, lowest_boundary_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    int(section["section"]),
                    str(section["label"]),
                    str(section["document_type"]),
                    str(section["summary"]),
                    str(section["date"]),
                    json.dumps(section["places"], ensure_ascii=False),
                    json.dumps(section["subjects"], ensure_ascii=False),
                    float(section["label_confidence"]),
                    float(section["lowest_boundary_confidence"]),
                ),
            )
            document_id = int(cursor.lastrowid)
            database.executemany(
                """INSERT INTO document_pages(document_id, page_id, position)
                VALUES (?, ?, ?)""",
                [
                    (document_id, run_pages[position], position)
                    for position in range(
                        int(section["start_page"]), int(section["end_page"]) + 1
                    )
                ],
            )
        database.execute(
            "UPDATE analysis_runs SET completed_at = ? WHERE id = ?",
            (_now(), run_id),
        )


def _latest_completed_run(database: sqlite3.Connection, collection_id: str) -> int | None:
    row = database.execute(
        """SELECT id FROM analysis_runs
        WHERE collection_id = ? AND completed_at IS NOT NULL
        ORDER BY id DESC LIMIT 1""",
        (collection_id,),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _review_order(
    database: sqlite3.Connection, collection_id: str, run_id: int
) -> list[int]:
    run_page_ids = [
        int(row["page_id"])
        for row in database.execute(
            "SELECT page_id FROM run_pages WHERE run_id = ? ORDER BY position", (run_id,)
        )
    ]
    reviewed_page_ids = [
        int(row["page_id"])
        for row in database.execute(
            """SELECT page_id FROM review_page_order
            WHERE collection_id = ? ORDER BY position""",
            (collection_id,),
        )
    ]
    if reviewed_page_ids:
        # Preserve the reviewer's relative order; newly discovered pages follow it.
        current = set(run_page_ids)
        reviewed = set(reviewed_page_ids)
        return ([page_id for page_id in reviewed_page_ids if page_id in current]
                + [page_id for page_id in run_page_ids if page_id not in reviewed])
    return run_page_ids


def _boundary_override_state(
    database: sqlite3.Connection, collection_id: str
) -> list[dict[str, int]]:
    return [
        {
            "left_page_id": int(row["left_page_id"]),
            "right_page_id": int(row["right_page_id"]),
            "decision": int(row["decision"]),
        }
        for row in database.execute(
            """SELECT left_page_id, right_page_id, decision FROM boundary_overrides
            WHERE collection_id = ? ORDER BY left_page_id, right_page_id""",
            (collection_id,),
        )
    ]


def _record_review_history(
    database: sqlite3.Connection, collection_id: str
) -> None:
    reviewed_order = [
        int(row["page_id"])
        for row in database.execute(
            """SELECT page_id FROM review_page_order
            WHERE collection_id = ? ORDER BY position""",
            (collection_id,),
        )
    ]
    database.execute(
        """INSERT INTO review_history(
            collection_id, page_order_json, boundary_overrides_json, created_at
        ) VALUES (?, ?, ?, ?)""",
        (
            collection_id,
            json.dumps(reviewed_order, separators=(",", ":")),
            json.dumps(_boundary_override_state(database, collection_id), separators=(",", ":")),
            _now(),
        ),
    )


def list_collections(output_dir: Path) -> list[dict[str, Any]]:
    with connect(output_dir) as database:
        rows = database.execute(
            """
            SELECT c.*,
                (SELECT COUNT(*) FROM run_pages rp
                 WHERE rp.run_id = (
                    SELECT ar.id FROM analysis_runs ar
                    WHERE ar.collection_id = c.id AND ar.completed_at IS NOT NULL
                    ORDER BY ar.id DESC LIMIT 1
                 )) AS page_count
            FROM collections c
            WHERE EXISTS (
                SELECT 1 FROM analysis_runs ar
                WHERE ar.collection_id = c.id AND ar.completed_at IS NOT NULL
            )
            """
        ).fetchall()
    items = [dict(row) for row in rows]
    return sorted(
        items,
        key=lambda item: [
            int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", str(item["relative_path"]))
        ],
    )


def load_collection_data(output_dir: Path, collection_id: str) -> dict[str, Any]:
    with connect(output_dir) as database:
        collection = database.execute(
            "SELECT * FROM collections WHERE id = ?", (collection_id,)
        ).fetchone()
        run_id = _latest_completed_run(database, collection_id)
        if collection is None or run_id is None:
            raise ValueError(f"Completed collection not found: {collection_id}")
        run_row = database.execute(
            "SELECT model FROM analysis_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run_row is not None
        run_page_rows = database.execute(
            """SELECT rp.position, p.id AS page_id, p.filename, pa.result_json, pa.created_at
            FROM run_pages rp
            JOIN pages p ON p.id = rp.page_id
            JOIN page_analyses pa ON pa.id = rp.analysis_id
            WHERE rp.run_id = ? ORDER BY rp.position""",
            (run_id,),
        ).fetchall()
        reviewed_order = _review_order(database, collection_id, run_id)
        page_rows_by_id = {int(row["page_id"]): row for row in run_page_rows}
        page_rows = [page_rows_by_id[page_id] for page_id in reviewed_order]
        boundary_rows = database.execute(
            "SELECT * FROM boundaries WHERE run_id = ? ORDER BY right_position",
            (run_id,),
        ).fetchall()
        override_rows = database.execute(
            """SELECT * FROM boundary_overrides
            WHERE collection_id = ?""",
            (collection_id,),
        ).fetchall()
        document_rows = database.execute(
            """SELECT d.*, MIN(dp.position) AS start_position, MAX(dp.position) AS end_position
            FROM documents d JOIN document_pages dp ON dp.document_id = d.id
            WHERE d.run_id = ? GROUP BY d.id ORDER BY d.document_number""",
            (run_id,),
        ).fetchall()
        document_page_rows = database.execute(
            """SELECT dp.document_id, dp.page_id FROM document_pages dp
            JOIN documents d ON d.id = dp.document_id
            WHERE d.run_id = ? ORDER BY d.document_number, dp.position""",
            (run_id,),
        ).fetchall()
        history_count = int(
            database.execute(
                "SELECT COUNT(*) FROM review_history WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()[0]
        )
        prompt_rows = database.execute(
            """SELECT rp.kind, pv.id, pv.content, pv.content_hash
            FROM run_prompts rp JOIN prompt_versions pv ON pv.id = rp.prompt_version_id
            WHERE rp.run_id = ? ORDER BY rp.kind""",
            (run_id,),
        ).fetchall()
        reviewed_label_rows = database.execute(
            """SELECT * FROM reviewed_labels
            WHERE collection_id = ? ORDER BY id""",
            (collection_id,),
        ).fetchall()
        stored_review_order = [int(row[0]) for row in database.execute(
            "SELECT page_id FROM review_page_order WHERE collection_id = ? ORDER BY position",
            (collection_id,),
        )]

    pages: list[dict[str, Any]] = []
    page_ids: dict[int, int] = {}
    for position, row in enumerate(page_rows, start=1):
        page = json.loads(row["result_json"])
        page.update(page=position, file=str(row["filename"]), page_id=int(row["page_id"]))
        pages.append(page)
        page_ids[position] = int(row["page_id"])

    original_by_pair: dict[tuple[int, int], dict[str, Any]] = {}
    for row in boundary_rows:
        original_by_pair[(int(row["left_page_id"]), int(row["right_page_id"]))] = {
            "boundary_id": int(row["id"]),
            "left_page_id": int(row["left_page_id"]),
            "right_page_id": int(row["right_page_id"]),
            "starts_new_document": bool(row["starts_new_document"]),
            "confidence": float(row["confidence"]),
            "reason": str(row["reason"]),
        }
    override_by_pair = {
        (int(row["left_page_id"]), int(row["right_page_id"])): row
        for row in override_rows
    }
    boundaries: list[dict[str, Any]] = []
    originals: list[dict[str, Any]] = []
    overrides: dict[str, dict[str, Any]] = {}
    for right_position in range(2, len(reviewed_order) + 1):
        pair = (reviewed_order[right_position - 2], reviewed_order[right_position - 1])
        original = dict(original_by_pair.get(pair, {
            "boundary_id": None,
            "left_page_id": pair[0],
            "right_page_id": pair[1],
            "starts_new_document": True,
            "confidence": 0.0,
            "reason": "Pages were made adjacent by a reviewer.",
        }))
        original["right_page"] = right_position
        originals.append(original)
        item = dict(original)
        override = override_by_pair.get(pair)
        if override is not None:
            item.update(
                starts_new_document=bool(override["decision"]),
                confidence=1.0,
                reason="Boundary set by a human reviewer.",
                review_source="human",
            )
            overrides[str(item["right_page"])] = {
                "starts_new_document": item["starts_new_document"],
                "reviewed_at": override["reviewed_at"],
            }
        boundaries.append(item)

    page_ids_by_document: dict[int, list[int]] = {}
    for row in document_page_rows:
        page_ids_by_document.setdefault(int(row["document_id"]), []).append(int(row["page_id"]))
    sections = [
        {
            "document_id": int(row["id"]),
            "section": int(row["document_number"]),
            "start_page": int(row["start_position"]),
            "end_page": int(row["end_position"]),
            "label": str(row["label"]),
            "document_type": str(row["document_type"]),
            "summary": str(row["summary"]),
            "date": str(row["date"]),
            "places": json.loads(row["places_json"]),
            "subjects": json.loads(row["subjects_json"]),
            "label_confidence": float(row["label_confidence"]),
            "lowest_boundary_confidence": float(row["lowest_boundary_confidence"]),
            "page_ids": page_ids_by_document[int(row["id"])],
        }
        for row in document_rows
    ]
    warnings = []
    current_ids = set(reviewed_order)
    adjacent_pairs = set(zip(reviewed_order, reviewed_order[1:]))
    if stored_review_order and set(stored_review_order) != current_ids:
        warnings.append("Source pages changed. Existing manual order is preserved; new pages are "
                        "appended and missing pages omitted. Check their placement.")
    if any((int(row['left_page_id']), int(row['right_page_id'])) not in adjacent_pairs
           for row in override_rows):
        warnings.append("Some saved boundary corrections no longer connect adjacent pages. "
                        "They are retained in the archive; review the changed grouping.")
    if any(not set(json.loads(row['page_ids_json'])).issubset(current_ids)
           for row in reviewed_label_rows):
        warnings.append("Some reviewed labels refer to missing pages. Those labels are retained "
                        "in the archive; check the affected documents.")
    current_groups: list[list[int]] = []
    for position, page_id in enumerate(reviewed_order, start=1):
        if position == 1 or boundaries[position - 2]['starts_new_document']:
            current_groups.append([])
        current_groups[-1].append(page_id)
    if any(json.loads(row['page_ids_json']) not in current_groups for row in reviewed_label_rows):
        warnings.append("Some saved reviewed labels no longer match the current document groups. "
                        "They are retained in the archive; check the affected metadata.")
    return {
        "collection": dict(collection),
        "run_id": run_id,
        "model": str(run_row["model"]),
        "pages": pages,
        "page_ids": page_ids,
        "boundaries": boundaries,
        "original_boundaries": originals,
        "sections": sections,
        "history_count": history_count,
        "review_warnings": warnings,
        "overrides": overrides,
        "prompts": {str(row["kind"]): dict(row) for row in prompt_rows},
        "reviewed_labels": {
            tuple(json.loads(row["page_ids_json"])): {
                "label": str(row["label"]),
                "document_type": str(row["document_type"]),
                "summary": str(row["summary"]),
                "date": str(row["date"]),
                "places": json.loads(row["places_json"]),
                "subjects": json.loads(row["subjects_json"]),
                "confidence": float(row["confidence"]),
                "reviewed_at": str(row["reviewed_at"]),
                "stale": any(
                    page_rows_by_id[page_id]['created_at'] > row['reviewed_at']
                    for page_id in json.loads(row['page_ids_json']) if page_id in page_rows_by_id
                ),
            }
            for row in reviewed_label_rows
        },
    }


def save_reviewed_label(
    output_dir: Path,
    collection_id: str,
    page_ids: list[int],
    label: dict[str, Any],
) -> None:
    """Save generated metadata for one exact, ordered reviewed page group."""
    if not page_ids:
        raise ValueError("A reviewed label must contain at least one page")
    with connect(output_dir) as database:
        database.execute(
            """INSERT INTO reviewed_labels(
                collection_id, page_ids_json, label, document_type, summary, date,
                places_json, subjects_json, confidence, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(collection_id, page_ids_json) DO UPDATE SET
                label = excluded.label,
                document_type = excluded.document_type,
                summary = excluded.summary,
                date = excluded.date,
                places_json = excluded.places_json,
                subjects_json = excluded.subjects_json,
                confidence = excluded.confidence,
                reviewed_at = excluded.reviewed_at""",
            (
                collection_id,
                json.dumps(page_ids, separators=(",", ":")),
                str(label["label"]),
                str(label["document_type"]),
                str(label["summary"]),
                str(label["date"]),
                json.dumps(label["places"], ensure_ascii=False),
                json.dumps(label["subjects"], ensure_ascii=False),
                float(label["confidence"]),
                _now(),
            ),
        )


def set_boundary_override(
    output_dir: Path, collection_id: str, right_position: int, decision: bool
) -> None:
    with connect(output_dir) as database:
        run_id = _latest_completed_run(database, collection_id)
        if run_id is None:
            raise ValueError("Completed collection not found")
        order = _review_order(database, collection_id, run_id)
        if right_position < 2 or right_position > len(order):
            raise ValueError("Boundary does not exist")
        left_page_id, right_page_id = order[right_position - 2:right_position]
        _record_review_history(database, collection_id)
        database.execute(
            """INSERT INTO boundary_overrides(
                collection_id, left_page_id, right_page_id, decision, reviewed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(collection_id, left_page_id, right_page_id) DO UPDATE SET
                decision = excluded.decision, reviewed_at = excluded.reviewed_at""",
            (collection_id, left_page_id, right_page_id, int(decision), _now()),
        )


def move_page_after(
    output_dir: Path, collection_id: str, page_position: int, target_position: int
) -> None:
    """Move one reviewed page after another and attach it to the target's section."""
    with connect(output_dir) as database:
        run_id = _latest_completed_run(database, collection_id)
        if run_id is None:
            raise ValueError("Completed collection not found")
        order = _review_order(database, collection_id, run_id)
        if not 1 <= page_position <= len(order) or not 1 <= target_position <= len(order):
            raise ValueError("Page number does not exist")
        if page_position == target_position:
            raise ValueError("A page cannot be moved after itself")
        moving_id = order[page_position - 1]
        target_id = order[target_position - 1]
        original_decisions = {
            (int(row["left_page_id"]), int(row["right_page_id"])): bool(
                row["starts_new_document"]
            )
            for row in database.execute(
                "SELECT * FROM boundaries WHERE run_id = ?", (run_id,)
            )
        }
        overrides = {
            (int(row["left_page_id"]), int(row["right_page_id"])): bool(row["decision"])
            for row in database.execute(
                "SELECT * FROM boundary_overrides WHERE collection_id = ?", (collection_id,)
            )
        }
        group_by_page: dict[int, int] = {}
        group = 0
        for index, page_id in enumerate(order):
            if index == 0 or overrides.get(
                (order[index - 1], page_id),
                original_decisions.get((order[index - 1], page_id), True),
            ):
                group += 1
            group_by_page[page_id] = group
        group_by_page[moving_id] = group_by_page[target_id]

        new_order = [page_id for page_id in order if page_id != moving_id]
        new_order.insert(new_order.index(target_id) + 1, moving_id)
        desired_decisions = {
            (left, right): group_by_page[left] != group_by_page[right]
            for left, right in zip(new_order, new_order[1:])
        }

        _record_review_history(database, collection_id)
        database.execute("DELETE FROM review_page_order WHERE collection_id = ?", (collection_id,))
        database.executemany(
            """INSERT INTO review_page_order(collection_id, page_id, position)
            VALUES (?, ?, ?)""",
            [
                (collection_id, page_id, position)
                for position, page_id in enumerate(new_order, start=1)
            ],
        )
        database.execute("DELETE FROM boundary_overrides WHERE collection_id = ?", (collection_id,))
        database.executemany(
            """INSERT INTO boundary_overrides(
                collection_id, left_page_id, right_page_id, decision, reviewed_at
            ) VALUES (?, ?, ?, ?, ?)""",
            [
                (collection_id, left, right, int(decision), _now())
                for (left, right), decision in desired_decisions.items()
                if decision != original_decisions.get((left, right), True)
            ],
        )


def undo_boundary_override(output_dir: Path, collection_id: str) -> bool:
    with connect(output_dir) as database:
        run_id = _latest_completed_run(database, collection_id)
        if run_id is None:
            return False
        event = database.execute(
            """SELECT * FROM review_history
            WHERE collection_id = ? ORDER BY id DESC LIMIT 1""",
            (collection_id,),
        ).fetchone()
        if event is None:
            return False
        database.execute("DELETE FROM review_page_order WHERE collection_id = ?", (collection_id,))
        page_order = json.loads(event["page_order_json"])
        database.executemany(
            """INSERT INTO review_page_order(collection_id, page_id, position)
            VALUES (?, ?, ?)""",
            [
                (collection_id, int(page_id), position)
                for position, page_id in enumerate(page_order, start=1)
            ],
        )
        database.execute("DELETE FROM boundary_overrides WHERE collection_id = ?", (collection_id,))
        previous_overrides = json.loads(event["boundary_overrides_json"])
        database.executemany(
            """INSERT INTO boundary_overrides(
                collection_id, left_page_id, right_page_id, decision, reviewed_at
            ) VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    collection_id, int(item["left_page_id"]), int(item["right_page_id"]),
                    int(item["decision"]), _now(),
                )
                for item in previous_overrides
            ],
        )
        database.execute("DELETE FROM review_history WHERE id = ?", (int(event["id"]),))
        return True
