"""Source snapshots and reusable stage results for incremental analysis."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import storage


def fingerprint(paths: list[Path]) -> list[dict[str, str]]:
    result = []
    for path in paths:
        with path.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        result.append({'file': path.name, 'sha256': digest})
    return result


def key(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def latest(output: Path, collection_id: str, *, completed: bool = True) -> dict[str, Any] | None:
    with storage.connect(output) as db:
        row = db.execute(
            'SELECT * FROM analysis_runs WHERE collection_id = ? '
            + ('AND completed_at IS NOT NULL ' if completed else '') + 'ORDER BY id DESC LIMIT 1',
            (collection_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        snapshot = db.execute('SELECT * FROM run_sources WHERE run_id = ?', (row['id'],)).fetchone()
        result['manifest'] = json.loads(snapshot['manifest_json']) if snapshot else None
        result['request'] = json.loads(snapshot['request_json']) if snapshot else {}
        result['pages'] = [dict(p) for p in db.execute(
            '''SELECT rp.position, rp.analysis_id, p.filename, pa.result_json
            FROM run_pages rp JOIN pages p ON p.id = rp.page_id
            JOIN page_analyses pa ON pa.id = rp.analysis_id
            WHERE rp.run_id = ? ORDER BY rp.position''', (row['id'],),
        )]
        result['boundaries'] = {int(b['right_position']): {
            'right_page': int(b['right_position']),
            'starts_new_document': bool(b['starts_new_document']),
            'confidence': b['confidence'], 'reason': b['reason'],
        } for b in db.execute('SELECT * FROM boundaries WHERE run_id = ?', (row['id'],))}
        result['documents'] = [dict(d) for d in db.execute(
            '''SELECT d.*, MIN(dp.position) AS start_page, MAX(dp.position) AS end_page
            FROM documents d JOIN document_pages dp ON dp.document_id = d.id
            WHERE d.run_id = ? GROUP BY d.id ORDER BY d.document_number''', (row['id'],),
        )]
        return result


def save_snapshot(output: Path, run_id: int, manifest: list[dict], request: dict) -> None:
    with storage.connect(output) as db:
        db.execute('INSERT INTO run_sources VALUES (?, ?, ?)',
                   (run_id, json.dumps(manifest), json.dumps(request)))


def adopt_legacy(output: Path, previous: dict, manifest: list[dict]) -> bool:
    """Adopt known unchanged legacy files without touching their analysis or review state."""
    if previous['manifest'] is not None:
        return False
    hashes = {p['file']: p['sha256'] for p in manifest}
    # Keep the old source set/order so additions, removals, and moves still trigger an update.
    baseline = [{'file': p['filename'], 'sha256': hashes.get(p['filename'], '')}
                for p in previous['pages']]
    with storage.connect(output) as db:
        db.execute('INSERT INTO run_sources VALUES (?, ?, ?)',
                   (previous['id'], json.dumps(baseline), '{}'))
        for page in previous['pages']:
            if page['filename'] not in hashes:
                continue
            db.execute('INSERT OR IGNORE INTO analysis_sources VALUES (?, ?)',
                       (page['analysis_id'], hashes[page['filename']]))
    previous['manifest'] = baseline
    return True


def cached_page(output: Path, collection_id: str, filename: str, sha256: str,
                analysis_key: str, *, minimum_run: int = 0,
                preferred: int | None = None) -> tuple[dict[str, Any], int] | None:
    with storage.connect(output) as db:
        row = db.execute(
            '''SELECT pa.id, pa.result_json FROM page_analyses pa
            JOIN pages p ON p.id = pa.page_id
            JOIN analysis_sources s ON s.analysis_id = pa.id
            WHERE p.collection_id = ? AND p.filename = ? AND s.sha256 = ?
                AND pa.run_id >= ? AND (pa.analysis_key = ? OR pa.id = ?)
            ORDER BY (pa.id = ?) DESC, pa.id DESC LIMIT 1''',
            (collection_id, filename, sha256, minimum_run, analysis_key, preferred, preferred),
        ).fetchone()
    return (json.loads(row['result_json']), int(row['id'])) if row else None


def remember(output: Path, run_id: int, stage: str, input_key: str, model: str, result: Any) -> None:
    with storage.connect(output) as db:
        db.execute('INSERT OR REPLACE INTO stage_results VALUES (?, ?, ?, ?, ?)',
                   (run_id, stage, input_key, model, json.dumps(result, ensure_ascii=False)))


def copy_boundaries(output: Path, previous_run: int, run_id: int) -> None:
    with storage.connect(output) as db:
        db.execute(
            '''INSERT INTO stage_results SELECT ?, stage, input_key, model, result_json
            FROM stage_results WHERE run_id = ? AND stage IN ('boundaries', 'boundary-review')''',
            (run_id, previous_run),
        )


def reuse(output: Path, collection_id: str, stage: str, input_key: str,
          *, minimum_run: int = 0) -> tuple[Any, str] | None:
    with storage.connect(output) as db:
        row = db.execute(
            '''SELECT s.result_json, s.model FROM stage_results s
            JOIN analysis_runs r ON r.id = s.run_id
            WHERE r.collection_id = ? AND s.stage = ? AND s.input_key = ?
                AND s.run_id >= ?
            ORDER BY s.run_id DESC LIMIT 1''',
            (collection_id, stage, input_key, minimum_run),
        ).fetchone()
    return (json.loads(row['result_json']), row['model']) if row else None


def label_key(group: list[dict], hashes: dict[str, str]) -> str:
    # Absolute page/section numbers can change without changing a document's content.
    return key([{'source': hashes.get(p['file']), 'page': {
        k: v for k, v in p.items() if k not in {'page', 'page_id', 'model'}
    }} for p in group])


def seed_labels(output: Path, previous: dict) -> None:
    """Make existing document labels reusable, including pre-migration results."""
    if previous['manifest'] is None:
        return
    hashes = {p['file']: p['sha256'] for p in previous['manifest']}
    pages = [json.loads(p['result_json']) for p in previous['pages']]
    for doc in previous['documents']:
        group = pages[doc['start_page'] - 1:doc['end_page']]
        value = {k: doc[k] for k in ('label', 'document_type', 'summary', 'date')}
        value.update(places=json.loads(doc['places_json']), subjects=json.loads(doc['subjects_json']),
                     confidence=doc['label_confidence'])
        input_key = label_key(group, hashes)
        with storage.connect(output) as db:
            db.execute('INSERT OR IGNORE INTO stage_results VALUES (?, ?, ?, ?, ?)',
                       (previous['id'], 'labels', input_key, previous['model'], json.dumps(value)))
