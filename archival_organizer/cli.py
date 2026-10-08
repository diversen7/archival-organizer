from __future__ import annotations

import argparse
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from openai import AuthenticationError, PermissionDeniedError

from . import incremental, storage
from .ai import (
    ArchiveAI, BOUNDARY_SCHEMA, PAGE_SCHEMA, boundary_request_prompt, boundary_review_request_prompt,
)
from .core import (
    build_sections,
    discover_collections,
    discover_pages,
    group_pages,
    suspicious_split_reasons,
    write_sections_csv,
)
from .labeling import label_targets, regenerate_collection_labels
from .prompts import (
    BOUNDARY_PROMPT,
    BOUNDARY_REVIEW_PROMPT,
    LABEL_PROMPT,
    PAGE_ANALYSIS_PROMPT,
)

DEFAULT_MODEL = "gpt-6-luna"


class PageAnalysisError(RuntimeError):
    """One or more pages failed, so the collection cannot be finalized."""


def _analysis_key() -> str:
    material = PAGE_ANALYSIS_PROMPT + json.dumps(PAGE_SCHEMA, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _prompt_definitions() -> dict[str, str]:
    return {
        "page_analysis": PAGE_ANALYSIS_PROMPT,
        "boundary": BOUNDARY_PROMPT,
        "boundary_review": BOUNDARY_REVIEW_PROMPT,
        "label": LABEL_PROMPT,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze archival scans or regenerate reviewed section metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser(
        "analyze",
        help="Analyze scans and divide them into documents",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    analyze.add_argument(
        "input",
        type=Path,
        help="Image directory, or a parent whose image-containing folders are separate collections",
    )
    analyze.add_argument(
        "--output", type=Path, default=Path("output"), help="Result and cache directory"
    )
    analyze.add_argument(
        "--collection",
        action="append",
        default=[],
        metavar="PATH",
        help="Analyze this folder and its descendant collections relative to input (use . for all); "
        "may be repeated",
    )
    analyze.add_argument(
        "--model",
        default=os.getenv("ARCHIVAL_MODEL", DEFAULT_MODEL),
        help="OpenAI model for scanning, boundaries, and labels",
    )
    analyze.add_argument(
        "--limit", type=int, help="Analyze only the first N pages in each collection (useful for testing)"
    )
    analyze.add_argument(
        "--refresh-pages",
        type=_page_selection,
        default=frozenset(),
        metavar="PAGES",
        help="Reanalyze selected page numbers, for example 38 or 38,42-44",
    )
    analyze.add_argument(
        "--refresh-boundaries", action="store_true",
        help="Recompute all boundary decisions and focused reviews, replacing cached responses",
    )
    analyze.add_argument(
        "--refresh-labels", action="store_true",
        help="Regenerate document labels while retaining extraction and grouping",
    )
    analyze.add_argument(
        "--reanalyze", action="store_true",
        help="Redo extraction, grouping, and labels for selected collections",
    )
    analyze.add_argument(
        "--workers", type=_positive_int, default=4, help="Concurrent requests for uncached page analysis"
    )
    analyze.add_argument(
        "--boundary-batch", type=_positive_int, default=24, help="Transitions per boundary request"
    )
    analyze.add_argument(
        "--label-batch", type=_positive_int, default=16, help="Groups per labelling request"
    )

    relabel = commands.add_parser(
        "relabel",
        help="Regenerate metadata for review-adjusted sections",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    relabel.add_argument("output", type=Path, help="Completed output containing archive.sqlite3")
    relabel.add_argument(
        "--collection",
        action="append",
        default=[],
        metavar="PATH_OR_ID",
        help="Only process this collection path or ID; may be repeated",
    )
    relabel.add_argument(
        "--all",
        dest="all_reviewed",
        action="store_true",
        help="Regenerate every review-adjusted section, including already regenerated sections",
    )
    relabel.add_argument(
        "--dry-run", action="store_true", help="List affected sections without making model requests"
    )
    relabel.add_argument(
        "--model", help="Override the model recorded on each collection's latest run"
    )
    relabel.add_argument(
        "--label-batch", type=_positive_int, default=16, help="Groups per labelling request"
    )
    return parser.parse_args(argv)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _page_selection(value: str) -> frozenset[int]:
    pages: set[int] = set()
    try:
        for item in value.split(","):
            bounds = item.strip().split("-", 1)
            start = int(bounds[0])
            end = int(bounds[-1])
            if start < 1 or end < start:
                raise ValueError
            pages.update(range(start, end + 1))
    except (ValueError, IndexError) as exc:
        raise argparse.ArgumentTypeError(
            "must be page numbers or inclusive ranges, for example 38,42-44"
        ) from exc
    return frozenset(pages)


def _load_or_analyze_pages(
    paths: list[Path],
    output_dir: Path,
    collection_id: str,
    run_id: int,
    ai: ArchiveAI,
    model: str,
    workers: int,
    refresh_pages: frozenset[int] = frozenset(),
    *,
    source_hashes: dict[str, str] | None = None,
    preferred_analyses: dict[str, int] | None = None,
    resume_from: int = 0,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any] | None] = [None] * len(paths)
    uncached: list[tuple[int, Path]] = []
    warned_models: set[str] = set()
    analysis_key = _analysis_key()

    for number, path in enumerate(paths, start=1):
        if source_hashes is not None:
            cached = incremental.cached_page(
                output_dir, collection_id, path.name, source_hashes[path.name], analysis_key,
                minimum_run=resume_from if number in refresh_pages else 0,
                preferred=(preferred_analyses or {}).get(path.name) if number not in refresh_pages else None,
            ) if number not in refresh_pages or resume_from else None
        else:
            cached = None if number in refresh_pages else storage.load_cached_page(
                output_dir, collection_id, number, path.name, analysis_key
            )
        if cached is None:
            uncached.append((number, path))
            continue
        page, analysis_id = cached
        page.update(page=number, file=path.name)
        storage.attach_cached_page(output_dir, run_id, number, analysis_id)
        if page.get("model") != model:
            cached_model = page.get("model", "unknown")
            if cached_model not in warned_models:
                print(
                    f"Warning: reusing compatible page analysis from {cached_model}; "
                    f"new model requests use {model}.",
                    flush=True,
                )
                warned_models.add(cached_model)
        print(f"[{number}/{len(paths)}] cached: {path.name}", flush=True)
        pages[number - 1] = page

    def analyze(number: int, path: Path) -> tuple[dict[str, Any], float]:
        page_started = time.perf_counter()
        print(f"[{number}/{len(paths)}] scanning with {model}: {path.name}", flush=True)
        page = ai.analyze_page(path)
        page.update({
            "page": number,
            "file": path.name,
            "model": model,
        })
        return page, time.perf_counter() - page_started

    if uncached:
        active_workers = min(workers, len(uncached))
        print(
            f"[pages] analyzing {len(uncached)} uncached page(s) with "
            f"{active_workers} worker(s)...",
            flush=True,
        )
        failures: list[tuple[int, Path, Exception]] = []
        stop_submitting = False
        submitted = 0
        remaining = iter(uncached)
        # OpenAI's sync client uses a pooled httpx client and can serve concurrent threads.
        with ThreadPoolExecutor(max_workers=active_workers, thread_name_prefix="page-analysis") as executor:
            futures: dict[Future[tuple[dict[str, Any], float]], tuple[int, Path]] = {}
            while True:
                # Keep only active work queued so account errors can stop new requests.
                while not stop_submitting and len(futures) < active_workers:
                    item = next(remaining, None)
                    if item is None:
                        break
                    number, path = item
                    futures[executor.submit(analyze, number, path)] = item
                    submitted += 1
                if not futures:
                    break
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    number, path = futures.pop(future)
                    try:
                        page, elapsed = future.result()
                    except Exception as exc:
                        failures.append((number, path, exc))
                        print(
                            f"[{number}/{len(paths)}] failed: {path.name}: "
                            f"{type(exc).__name__}: {exc}", flush=True,
                        )
                        if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
                            if not stop_submitting:
                                print(
                                    "[pages] API authentication or permission error; stopping new "
                                    "pages and saving results from requests already running.",
                                    flush=True,
                                )
                            stop_submitting = True
                        continue
                    storage.save_page_analysis(
                        output_dir, collection_id, run_id, page, model, analysis_key,
                        source_hash=source_hashes[page["file"]] if source_hashes is not None else None,
                    )
                    pages[number - 1] = page
                    print(
                        f"[{number}/{len(paths)}] complete: {page['document_type']} — "
                        f"{page['title'] or 'untitled'}; {len(page['transcription']):,} characters "
                        f"({elapsed:.1f}s)",
                        flush=True,
                    )
        if failures:
            summary = [
                f"Page analysis failed for collection {collection_id}: {len(failures)} failed, "
                f"{sum(page is not None for page in pages)} successful/cached, "
                f"{len(uncached) - submitted} not attempted.",
                *[
                    f"  [{number}/{len(paths)}] {path.name}: {type(exc).__name__}: {exc}"
                    for number, path, exc in sorted(failures, key=lambda item: item[0])
                ],
                "Grouping, labeling, and final output for this collection were not run.",
                f"Successful page analyses are cached in {output_dir / storage.DATABASE_NAME}. "
                "Fix the errors and rerun to resume.",
            ]
            raise PageAnalysisError("\n".join(summary)) from failures[0][2]

    if any(page is None for page in pages):
        raise RuntimeError("Page analysis completed without producing every page")
    return [page for page in pages if page is not None]


def _cached_boundary_request(
    output_dir: Path,
    model: str,
    prompt: str,
    request: Callable[[], list[dict[str, Any]]],
    *,
    refresh: bool,
    description: str,
) -> list[dict[str, Any]]:
    material = json.dumps(
        {"model": model, "prompt": prompt, "schema": BOUNDARY_SCHEMA},
        sort_keys=True, ensure_ascii=False,
    )
    cache_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    cached = None if refresh else storage.load_boundary_batch(output_dir, cache_key)
    if cached is not None:
        print(f"[boundaries] cached: {description}", flush=True)
        return cached
    print(f"[boundaries] {description}...", flush=True)
    started = time.perf_counter()
    decisions = request()
    storage.save_boundary_batch(output_dir, cache_key, decisions)
    print(f"[boundaries] {description} complete ({time.perf_counter() - started:.1f}s)", flush=True)
    return decisions


def _process_collection(
    input_dir: Path,
    output_dir: Path,
    collection_id: str,
    args: argparse.Namespace,
    ai: ArchiveAI | None = None,
) -> tuple[int, int]:
    run_started = time.perf_counter()
    paths = discover_pages(input_dir)
    if args.limit:
        paths = paths[: args.limit]
    invalid_refresh_pages = sorted(args.refresh_pages - set(range(1, len(paths) + 1)))
    if invalid_refresh_pages:
        invalid = ", ".join(str(page) for page in invalid_refresh_pages)
        raise SystemExit(f"Refresh page numbers outside {input_dir}: {invalid}")

    manifest = incremental.fingerprint(paths)
    hashes = {item["file"]: item["sha256"] for item in manifest}
    previous = incremental.latest(output_dir, collection_id)
    pending = incremental.latest(output_dir, collection_id, completed=False)
    if previous and incremental.adopt_legacy(output_dir, previous, manifest):
        print(f"[collection] recorded source baseline for existing completed collection {collection_id}; "
              "analysis and review state preserved.", flush=True)
    request = {
        "pages": sorted(args.refresh_pages),
        "boundaries": args.refresh_boundaries or args.reanalyze,
        "labels": args.refresh_labels or args.reanalyze,
        "all_pages": args.reanalyze,
    }
    explicit = any(request.values())
    resuming = bool(not explicit and pending and pending["completed_at"] is None
                    and pending["manifest"] == manifest)
    if resuming:
        request = pending["request"]
    unchanged = previous is not None and previous["manifest"] == manifest
    if unchanged and not explicit and not resuming:
        print(f"[collection] skipped {collection_id}: completed, sources unchanged "
              f"(original run model: {previous['model']}).", flush=True)
        return len(paths), len(previous["documents"])

    reason = ("explicitly reanalyzing" if explicit else "resuming" if resuming else
              "updating changed sources" if previous else "starting new collection")
    print(f"[collection] {reason}: {collection_id}", flush=True)
    if ai is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit("Set OPENAI_API_KEY before running the analyzer.")
        ai = ArchiveAI(args.model)
    storage.sync_pages(output_dir, collection_id, paths)
    run_id = storage.start_run(
        output_dir, collection_id, args.model, _analysis_key(), _prompt_definitions()
    )
    resume_from = int(request.get("origin", pending["id"])) if resuming else 0
    request["origin"] = resume_from or run_id
    incremental.save_snapshot(output_dir, run_id, manifest, request)
    refresh_pages = frozenset(range(1, len(paths) + 1) if request["all_pages"] else request["pages"])
    preferred = {p["filename"]: p["analysis_id"] for p in previous["pages"]} if previous else {}
    if previous:
        incremental.seed_labels(output_dir, previous)
    pages = _load_or_analyze_pages(
        paths, output_dir, collection_id, run_id, ai, args.model, args.workers, refresh_pages,
        source_hashes=hashes, preferred_analyses=preferred, resume_from=resume_from,
    )

    def boundary_request(stage, prompt, invoke, description):
        input_key = incremental.key({"prompt": prompt, "schema": BOUNDARY_SCHEMA})
        forced = request["boundaries"]
        cached = incremental.reuse(
            output_dir, collection_id, stage, input_key,
            minimum_run=resume_from if forced else 0,
        ) if not forced or resuming else None
        if cached is not None:
            value, model = cached
            print(f"[boundaries] cached: {description} (from {model})", flush=True)
        else:
            value = _cached_boundary_request(
                output_dir, args.model, prompt, invoke, refresh=forced, description=description,
            )
            model = args.model
        incremental.remember(output_dir, run_id, stage, input_key, model, value)
        return value

    if unchanged and not refresh_pages and not request["boundaries"]:
        # Label-only requests must not reconsider extraction or grouping.
        boundaries = previous["boundaries"]
        incremental.copy_boundaries(output_dir, previous["id"], run_id)
    else:
        boundaries: dict[int, dict] = {}
        # Each batch includes one left-context page, so every internal transition is evaluated once.
        for start in range(0, len(pages) - 1, args.boundary_batch):
            window = pages[start : min(len(pages), start + args.boundary_batch + 1)]
            returned = boundary_request(
                "boundaries", boundary_request_prompt(window),
                lambda: ai.decide_boundaries(window),
                description=f"deciding pages {window[0]['page']}-{window[-1]['page']}",
            )
            for item in returned:
                right = int(item["right_page"])
                if window[0]["page"] < right <= window[-1]["page"]:
                    boundaries[right] = item
            for right in range(window[0]["page"] + 1, window[-1]["page"] + 1):
                boundaries.setdefault(right, {
                    "right_page": right,
                    "starts_new_document": True,
                    "confidence": 0.0,
                    "reason": "Model omitted this boundary; conservative fallback requires review.",
                })
        audit_reasons = suspicious_split_reasons(pages, boundaries)
        if audit_reasons:
            page_list = ", ".join(str(page) for page in audit_reasons)
            returned = boundary_request(
                "boundary-review", boundary_review_request_prompt(pages, boundaries, audit_reasons),
                lambda: ai.review_boundaries(pages, boundaries, audit_reasons),
                description=f"rechecking contradictory splits before pages {page_list}",
            )
            reviewed = {
                int(item["right_page"]): item
                for item in returned
                if int(item["right_page"]) in audit_reasons
            }
            for right_page, reasons in audit_reasons.items():
                if right_page in reviewed:
                    boundaries[right_page] = reviewed[right_page]
                if boundaries[right_page].get("starts_new_document"):
                    # A contradictory split may be legitimate, but it should never bypass human review.
                    boundaries[right_page]["confidence"] = min(
                        float(boundaries[right_page].get("confidence", 0)), 0.74
                    )
                    boundaries[right_page]["reason"] += " Audit flags: " + "; ".join(reasons) + "."

    groups = group_pages(pages, boundaries)
    labels = [None] * len(groups)
    missing = []
    for index, group in enumerate(groups):
        input_key = incremental.label_key(group, hashes)
        forced = request["labels"]
        cached = incremental.reuse(
            output_dir, collection_id, "labels", input_key,
            minimum_run=resume_from if forced else 0,
        ) if not forced or resuming else None
        if cached is None:
            missing.append((index, group, input_key))
        else:
            label, model = cached
            labels[index] = dict(label, group=index + 1)
            incremental.remember(output_dir, run_id, "labels", input_key, model, label)
            print(f"[labels] cached: group {index + 1} (from {model})", flush=True)
    for start in range(0, len(missing), args.label_batch):
        batch = missing[start:start + args.label_batch]
        print(f"[labels] labeling {len(batch)} changed/unfinished group(s)...", flush=True)
        returned = ai.label_groups([group for _, group, _ in batch], 1)
        by_number = {int(item["group"]): item for item in returned}
        for number, (index, _group, input_key) in enumerate(batch, start=1):
            if number not in by_number:
                raise ValueError(f"Model omitted label {number}; collection remains unfinished.")
            label = {k: v for k, v in by_number[number].items() if k != "group"}
            incremental.remember(output_dir, run_id, "labels", input_key, args.model, label)
            labels[index] = dict(label, group=index + 1)

    current_paths = discover_pages(input_dir)
    if args.limit:
        current_paths = current_paths[:args.limit]
    if incremental.fingerprint(current_paths) != manifest:
        raise ValueError("Source images changed during analysis; rerun to update them. Previous results preserved.")
    sections = build_sections(groups, labels, boundaries)
    storage.save_results(output_dir, collection_id, sections, boundaries, run_id)
    reviewed = storage.load_collection_data(output_dir, collection_id)
    for warning in reviewed["review_warnings"]:
        print(f"[review] {warning}", flush=True)
    if any(label.get("stale") for label in reviewed["reviewed_labels"].values()):
        print("[review] Page extraction changed beneath a reviewed label; check its metadata.", flush=True)
    export_name = "sections.csv" if collection_id == "default" else f"sections-{collection_id}.csv"
    write_sections_csv(output_dir / export_name, sections)
    print(
        f"Done: {len(paths)} pages grouped into {len(groups)} sections in "
        f"{time.perf_counter() - run_started:.1f}s. Database: {output_dir / storage.DATABASE_NAME}",
        flush=True,
    )
    return len(paths), len(groups)


def _run_analysis(args: argparse.Namespace) -> None:
    if not args.input.is_dir():
        raise SystemExit(f"Input directory does not exist: {args.input}")

    input_root = args.input.resolve()
    collections = discover_collections(input_root)
    if not collections:
        raise SystemExit(f"No supported images found in {args.input} or its subdirectories")
    # Determine identity before filtering so selected collections retain their existing IDs.
    multi_collection = len(collections) > 1 or collections[0] != input_root
    discovered_count = len(collections)
    if args.collection:
        selected = set()
        for selector in args.collection:
            relative = Path(selector)
            if not selector or relative.is_absolute() or ".." in relative.parts:
                raise SystemExit(f"Collection path must be relative to input without '..': {selector!r}")
            matches = [
                path for path in collections
                if path.relative_to(input_root).is_relative_to(relative)
            ]
            if not matches:
                raise SystemExit(
                    f"Collection not found: {selector!r}. Select a folder containing images "
                    f"or descendant collections relative to {input_root}."
                )
            selected.update(matches)
        collections = [path for path in collections if path in selected]
    if len(collections) > 1 and args.refresh_pages:
        raise SystemExit(
            "--refresh-pages requires exactly one collection. "
            "Use --collection PATH to select it while keeping the same input root."
        )
    storage.initialize(args.output)
    if not multi_collection:
        storage.upsert_collection(args.output, "default", input_root.name, ".", input_root)
        _process_collection(input_root, args.output, "default", args)
        return

    print(
        f"Found {discovered_count} image collections under {input_root}; "
        f"processing {len(collections)}", flush=True,
    )
    for index, collection_dir in enumerate(collections, start=1):
        relative_input = collection_dir.relative_to(input_root)
        collection_id = hashlib.sha256(relative_input.as_posix().encode("utf-8")).hexdigest()[:12]
        print(
            f"\n[collection {index}/{len(collections)}] {relative_input.as_posix() or '.'}",
            flush=True,
        )
        storage.upsert_collection(
            args.output,
            collection_id,
            collection_dir.name,
            relative_input.as_posix() or ".",
            collection_dir,
        )
        _process_collection(collection_dir, args.output, collection_id, args)

    print(f"Selected collections complete. Database: {args.output / storage.DATABASE_NAME}", flush=True)


def _selected_collections(
    output_dir: Path, selectors: list[str]
) -> list[dict[str, Any]]:
    collections = storage.list_collections(output_dir)
    if not collections:
        raise SystemExit(f"Archive contains no completed collections: {output_dir}")
    if not selectors:
        return collections

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for selector in selectors:
        matches = [
            item for item in collections
            if selector in (str(item["id"]), str(item["relative_path"]))
        ]
        if not matches:
            raise SystemExit(f"Collection not found: {selector}")
        item = matches[0]
        collection_id = str(item["id"])
        if collection_id not in seen:
            selected.append(item)
            seen.add(collection_id)
    return selected


def _run_relabel(args: argparse.Namespace) -> None:
    output_dir = args.output.resolve()
    database_path = output_dir / storage.DATABASE_NAME
    if not database_path.is_file():
        raise SystemExit(f"Archive database does not exist: {database_path}")
    storage.initialize(output_dir)
    collections = _selected_collections(output_dir, args.collection)

    plans: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    total = 0
    for collection in collections:
        collection_id = str(collection["id"])
        targets = label_targets(
            output_dir, collection_id, all_reviewed=args.all_reviewed
        )
        plans.append((collection, targets))
        if targets:
            print(
                f"[relabel] {collection['relative_path']} ({collection_id}): "
                f"{len(targets)} section(s)",
                flush=True,
            )
            for section in targets:
                print(
                    f"  section {section['section']}: pages "
                    f"{section['start_page']}-{section['end_page']}",
                    flush=True,
                )
        total += len(targets)

    if total == 0:
        print("No review-adjusted sections need regenerated metadata.", flush=True)
        return
    if args.dry_run:
        print(f"Dry run: {total} section(s) would be regenerated.", flush=True)
        return
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY before regenerating section metadata.")

    saved_total = 0
    for collection, targets in plans:
        if not targets:
            continue
        requested, saved = regenerate_collection_labels(
            output_dir,
            str(collection["id"]),
            all_reviewed=args.all_reviewed,
            model=args.model,
            label_batch=args.label_batch,
            report=lambda message, path=collection["relative_path"]: print(
                f"[{path}] {message}", flush=True
            ),
        )
        saved_total += saved
        if saved != requested:
            print(
                f"Warning: {requested - saved} section(s) in {collection['relative_path']} "
                "remain marked for review.",
                flush=True,
            )
    print(
        f"Done: regenerated metadata for {saved_total} of {total} section(s).",
        flush=True,
    )


def main() -> None:
    args = parse_args()
    if args.command == "analyze":
        try:
            _run_analysis(args)
        except PageAnalysisError as exc:
            raise SystemExit(str(exc)) from None
    else:
        _run_relabel(args)
