from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import storage
from .ai import ArchiveAI
from .review import effective_sections, write_reviewed_sections


def reviewed_sections(
    output_dir: Path, collection_id: str
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]], str]:
    """Load the effective reviewed grouping for one completed collection."""
    data = storage.load_collection_data(output_dir, collection_id)
    pages = {int(page["page"]): page for page in data["pages"]}
    boundaries = {int(item["right_page"]): item for item in data["boundaries"]}
    sections = effective_sections(
        data["sections"], pages, boundaries, data["reviewed_labels"]
    )
    return pages, sections, str(data["model"])


def label_targets(
    output_dir: Path, collection_id: str, *, all_reviewed: bool = False
) -> list[dict[str, Any]]:
    """Return adjusted groups that need labels, or every adjusted group when requested."""
    _pages, sections, _model = reviewed_sections(output_dir, collection_id)
    if all_reviewed:
        return [section for section in sections if section.get("review_adjusted")]
    return [section for section in sections if section.get("label_needs_review")]


def regenerate_collection_labels(
    output_dir: Path,
    collection_id: str,
    *,
    all_reviewed: bool = False,
    model: str | None = None,
    label_batch: int = 16,
    ai: Any | None = None,
    report: Callable[[str], None] = print,
) -> tuple[int, int]:
    """Regenerate metadata for selected reviewed groups and return requested/saved counts."""
    if label_batch < 1:
        raise ValueError("label_batch must be at least 1")
    pages, sections, stored_model = reviewed_sections(output_dir, collection_id)
    if all_reviewed:
        targets = [section for section in sections if section.get("review_adjusted")]
    else:
        targets = [section for section in sections if section.get("label_needs_review")]
    if not targets:
        return 0, 0

    labeler = ai or ArchiveAI(model or stored_model)
    numbered_groups = [
        (
            int(section["section"]),
            [
                pages[number]
                for number in range(int(section["start_page"]), int(section["end_page"]) + 1)
            ],
        )
        for section in targets
    ]
    saved = 0
    for start in range(0, len(numbered_groups), label_batch):
        batch = numbered_groups[start : start + label_batch]
        returned = labeler.label_numbered_groups(batch)
        labels = {int(label["group"]): label for label in returned}
        for section_number, group in batch:
            label = labels.get(section_number)
            if label is None:
                report(f"[labels] section {section_number} omitted by model; left for review")
                continue
            storage.save_reviewed_label(
                output_dir,
                collection_id,
                [int(page["page_id"]) for page in group],
                label,
            )
            saved += 1
            report(
                f"[labels] section {section_number} pages "
                f"{group[0]['page']}-{group[-1]['page']}: {label['label']}"
            )

    _pages, updated_sections, _model = reviewed_sections(output_dir, collection_id)
    suffix = "" if collection_id == "default" else f"-{collection_id}"
    write_reviewed_sections(
        output_dir / f"reviewed-sections{suffix}.csv", updated_sections
    )
    return len(targets), saved
