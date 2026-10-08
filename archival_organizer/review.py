from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def effective_sections(
    original_sections: list[dict[str, Any]],
    pages: dict[int, dict[str, Any]],
    boundaries: dict[int, dict[str, Any]],
    reviewed_labels: dict[tuple[int, ...], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reviewed_labels = reviewed_labels or {}
    original_by_page_id: dict[int, dict[str, Any]] = {}
    for section in original_sections:
        section_page_ids = section.get("page_ids") or [
            int(pages[number]["page_id"])
            for number in range(int(section["start_page"]), int(section["end_page"]) + 1)
        ]
        section["page_ids"] = section_page_ids
        for page_id in section_page_ids:
            original_by_page_id[int(page_id)] = section

    groups: list[list[int]] = []
    for number in sorted(pages):
        if number == 1 or boundaries.get(number, {}).get("starts_new_document", True):
            groups.append([])
        groups[-1].append(number)

    sections: list[dict[str, Any]] = []
    for section_number, group in enumerate(groups, start=1):
        sources: list[dict[str, Any]] = []
        for number in group:
            source = original_by_page_id[int(pages[number]["page_id"])]
            if not sources or sources[-1] is not source:
                sources.append(source)
        start, end = group[0], group[-1]
        group_page_ids = [int(pages[number]["page_id"]) for number in group]
        unchanged = (
            len(sources) == 1
            and group_page_ids == [int(page_id) for page_id in sources[0]["page_ids"]]
        )
        if unchanged:
            section = dict(sources[0])
            section["section"] = section_number
            section["start_page"] = start
            section["end_page"] = end
            section["files"] = [str(pages[number]["file"]) for number in group]
            section["review_adjusted"] = False
            section["label_needs_review"] = False
        else:
            document_types = {str(item.get("document_type") or "other") for item in sources}
            section = {
                "section": section_number,
                "start_page": start,
                "end_page": end,
                "files": [str(pages[number]["file"]) for number in group],
                "label": str(sources[0].get("label") or "Unlabelled document"),
                "document_type": document_types.pop() if len(document_types) == 1 else "mixed",
                "summary": " ".join(
                    dict.fromkeys(str(item.get("summary") or "") for item in sources)
                ).strip(),
                "date": " / ".join(
                    dict.fromkeys(
                        str(item.get("date") or "") for item in sources if item.get("date")
                    )
                ),
                "places": list(
                    dict.fromkeys(
                        str(place) for item in sources for place in item.get("places", [])
                    )
                ),
                "subjects": list(
                    dict.fromkeys(
                        str(subject) for item in sources for subject in item.get("subjects", [])
                    )
                ),
                "label_confidence": 0.0,
                "review_adjusted": True,
                "label_needs_review": True,
            }
        group_key = tuple(group_page_ids)
        reviewed_label = reviewed_labels.get(group_key)
        if reviewed_label is not None:
            section.update(
                label=str(reviewed_label["label"]),
                document_type=str(reviewed_label["document_type"]),
                summary=str(reviewed_label["summary"]),
                date=str(reviewed_label["date"]),
                places=list(reviewed_label["places"]),
                subjects=list(reviewed_label["subjects"]),
                label_confidence=float(reviewed_label["confidence"]),
                label_needs_review=bool(reviewed_label.get("stale")),
                label_source="regenerated",
                review_adjusted=True,
            )
        section["lowest_boundary_confidence"] = min(
            (
                float(boundaries.get(number, {}).get("confidence", 0))
                for number in range(max(2, start), end + 1)
            ),
            default=1.0,
        )
        sections.append(section)
    return sections


def write_reviewed_sections(path: Path, sections: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "section",
                "pages",
                "type",
                "label",
                "date",
                "places",
                "confidence",
                "needs_review",
                "human_adjusted",
            ]
        )
        for section in sections:
            confidence = min(
                float(section.get("label_confidence", 0)),
                float(section.get("lowest_boundary_confidence", 0)),
            )
            writer.writerow(
                [
                    section["section"],
                    f'{section["start_page"]}-{section["end_page"]}',
                    section["document_type"],
                    section["label"],
                    section.get("date", ""),
                    "; ".join(section.get("places", [])),
                    f"{confidence:.2f}",
                    "yes"
                    if confidence < 0.75 or section.get("label_needs_review")
                    else "no",
                    "yes" if section.get("review_adjusted") else "no",
                ]
            )
    temporary.replace(path)
