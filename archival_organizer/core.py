from __future__ import annotations

import csv
from difflib import SequenceMatcher
import re
from pathlib import Path
from typing import Any

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


def natural_key(path: Path) -> list[str | int]:
    """Sort scan_9 before scan_10 while retaining the physical scan order."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def discover_pages(input_dir: Path) -> list[Path]:
    pages = [p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES]
    return sorted(pages, key=natural_key)


def discover_collections(input_dir: Path) -> list[Path]:
    """Find every directory at or below *input_dir* that directly contains images."""
    input_dir = input_dir.resolve()
    collections = {
        path.parent
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    }
    return sorted(
        collections,
        key=lambda path: [natural_key(Path(part)) for part in path.relative_to(input_dir).parts],
    )


def group_pages(pages: list[dict[str, Any]], boundaries: dict[int, dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group pages. Boundary keys are 1-based right-hand page numbers."""
    groups: list[list[dict[str, Any]]] = []
    for page in pages:
        number = int(page["page"])
        starts_new = number == 1 or boundaries.get(number, {}).get("starts_new_document", True)
        if starts_new or not groups:
            groups.append([])
        groups[-1].append(page)
    return groups


def suspicious_split_reasons(
    pages: list[dict[str, Any]], boundaries: dict[int, dict[str, Any]]
) -> dict[int, list[str]]:
    """Find splits whose page evidence contradicts a confident new-document decision."""
    def page_number_value(page: dict[str, Any]) -> int | None:
        match = re.search(r"\d+", str(page.get("printed_page_number", "")))
        return int(match.group()) if match else None

    def hierarchical_number(page: dict[str, Any]) -> tuple[int, int] | None:
        match = re.fullmatch(
            r"\s*(\d+)\s*[.]\s*(\d+)\s*", str(page.get("printed_page_number", ""))
        )
        return (int(match.group(1)), int(match.group(2))) if match else None

    def normalized(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    def shares_document_masthead(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_org = normalized(left.get("organization"))
        right_org = normalized(right.get("organization"))
        if not left_org or left_org != right_org:
            return False

        # Page analysis separates multi-cell headers with pipes. The last cell is
        # commonly a changing chapter title, so compare the stable cells before it.
        left_parts = [normalized(part) for part in str(left.get("header_text", "")).split("|")]
        right_parts = [normalized(part) for part in str(right.get("header_text", "")).split("|")]
        if (
            len(left_parts) < 3
            or len(right_parts) < 3
            or not left_parts[1]
            or not right_parts[1]
            or left_parts[0] != right_parts[0]
        ):
            return False
        return SequenceMatcher(None, left_parts[1], right_parts[1]).ratio() >= 0.9

    by_number = {int(page["page"]): page for page in pages}
    last_page = max(by_number, default=0)
    suspicious: dict[int, list[str]] = {}
    for right_page, decision in boundaries.items():
        if not decision.get("starts_new_document") or right_page <= 1:
            continue
        left = by_number.get(right_page - 1)
        right = by_number.get(right_page)
        if left is None or right is None:
            continue

        left_number = page_number_value(left)
        right_number = page_number_value(right)
        pagination_supports_join = right_number == 2 or (
            left_number is not None and right_number == left_number + 1
        )
        sentence_seam = bool(left.get("ends_mid_sentence") and right.get("starts_mid_sentence"))

        reasons: list[str] = []
        if sentence_seam:
            reasons.append("left ends mid-sentence and right starts mid-sentence")
            if (
                left_number is not None
                and right_number is not None
                and right_number != left_number + 1
            ):
                reasons.append(
                    f"reported pagination conflicts ({left_number} to {right_number}) and may be an OCR error"
                )

        left_ending = str(left.get("ending_text") or left.get("transcription", "")).rstrip()
        right_transcription = str(right.get("transcription", ""))
        starts_numbered_section = bool(re.match(r"^\s*(?:\d+|[A-ZÆØÅ])[.)]\s+", right_transcription))
        if left_ending.endswith(":") and starts_numbered_section:
            reasons.append("left ends with a colon introducing the numbered section on the right")

        if right.get("page_role") == "middle" and pagination_supports_join:
            reasons.append(f"pagination supports continuation onto internal page {right_number}")

        left_hierarchy = hierarchical_number(left)
        right_hierarchy = hierarchical_number(right)
        if (
            left_hierarchy is not None
            and right_hierarchy == (left_hierarchy[0] + 1, 0)
            and shares_document_masthead(left, right)
        ):
            reasons.append(
                f"hierarchical numbering progresses from {left_hierarchy[0]}.{left_hierarchy[1]} "
                f"to {right_hierarchy[0]}.0 under the same document masthead"
            )

        next_is_split = right_page == last_page or boundaries.get(right_page + 1, {}).get(
            "starts_new_document", True
        )
        if reasons and next_is_split and right.get("page_role") == "middle" and (
            right.get("starts_mid_sentence") or right.get("ends_mid_sentence")
        ):
            reasons.append("new section would be a single page classified as a middle page")

        if reasons:
            suspicious[right_page] = reasons

    # A page between N and N+2 is likely a misread N+1 even when its own extracted
    # number is very different (for example, a damaged 17 read as 47). Review both
    # edges because the bad number can otherwise isolate the page from each neighbor.
    for page_number in range(2, last_page):
        previous = by_number[page_number - 1]
        current = by_number[page_number]
        following = by_number[page_number + 1]
        previous_number = page_number_value(previous)
        current_number = page_number_value(current)
        following_number = page_number_value(following)
        if (
            previous_number is None
            or current_number is None
            or following_number != previous_number + 2
            or current_number == previous_number + 1
            or current.get("page_role") != "middle"
        ):
            continue
        reason = (
            f"surrounding pagination {previous_number}, {current_number}, {following_number} "
            f"suggests the middle number may be a misread {previous_number + 1}"
        )
        for right_page in (page_number, page_number + 1):
            if boundaries.get(right_page, {}).get("starts_new_document"):
                suspicious.setdefault(right_page, []).append(reason)
    return suspicious


def build_sections(
    groups: list[list[dict[str, Any]]],
    labels: list[dict[str, Any]],
    boundaries: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for index, (group, label) in enumerate(zip(groups, labels, strict=True), start=1):
        start, end = group[0]["page"], group[-1]["page"]
        decision_confidences = [
            boundaries[p]["confidence"] for p in range(max(2, start), end + 1) if p in boundaries
        ]
        sections.append(
            {
                "section": index,
                "start_page": start,
                "end_page": end,
                "files": [p["file"] for p in group],
                "label": label["label"],
                "document_type": label["document_type"],
                "summary": label["summary"],
                "date": label["date"],
                "places": label["places"],
                "subjects": label["subjects"],
                "label_confidence": label["confidence"],
                "lowest_boundary_confidence": min(decision_confidences, default=1.0),
            }
        )

    return sections


def write_sections_csv(path: Path, sections: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["section", "pages", "type", "label", "date", "places", "confidence", "needs_review"]
        )
        for section in sections:
            confidence = min(section["label_confidence"], section["lowest_boundary_confidence"])
            writer.writerow(
                [
                    section["section"],
                    f'{section["start_page"]}-{section["end_page"]}',
                    section["document_type"],
                    section["label"],
                    section["date"],
                    "; ".join(section["places"]),
                    f"{confidence:.2f}",
                    "yes" if confidence < 0.75 else "no",
                ]
            )
