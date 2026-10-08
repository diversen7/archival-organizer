from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image, ImageOps

from .prompts import (
    PAGE_ANALYSIS_PROMPT,
    boundary_prompt,
    boundary_review_prompt,
    label_prompt,
)

PAGE_TYPES = [
    "article", "map", "letter", "report", "minutes", "form", "brochure",
    "newspaper", "photograph", "drawing", "book_page", "cover_or_divider",
    "invoice", "envelope", "note", "blank", "other",
]

PAGE_ROLES = ["first", "middle", "last", "single", "cover", "appendix", "attachment", "unknown"]


def _schema(name: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


PAGE_SCHEMA = _schema(
    "archive_page",
    {
        "document_type": {"type": "string", "enum": PAGE_TYPES},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "language": {"type": "string"},
        "printed_page_number": {"type": "string"},
        "printed_total_pages": {"type": "string"},
        "page_role": {"type": "string", "enum": PAGE_ROLES},
        "date": {"type": "string"},
        "author": {"type": "string"},
        "sender": {"type": "string"},
        "recipient": {"type": "string"},
        "organization": {"type": "string"},
        "reference_number": {"type": "string"},
        "section_heading": {"type": "string"},
        "places": {"type": "array", "items": {"type": "string"}},
        "subjects": {"type": "array", "items": {"type": "string"}},
        "header_text": {"type": "string"},
        "footer_text": {"type": "string"},
        "beginning_text": {"type": "string"},
        "ending_text": {"type": "string"},
        "starts_mid_sentence": {"type": "boolean"},
        "ends_mid_sentence": {"type": "boolean"},
        "continuation_marker": {"type": "string"},
        "possible_attachment": {"type": "boolean"},
        "layout_signature": {"type": "string"},
        "visual_description": {"type": "string"},
        "transcription": {"type": "string"},
        "extraction_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    [
        "document_type", "title", "summary", "language", "printed_page_number",
        "printed_total_pages", "page_role", "date", "author", "sender", "recipient",
        "organization", "reference_number", "section_heading", "places", "subjects",
        "header_text", "footer_text", "beginning_text", "ending_text", "starts_mid_sentence",
        "ends_mid_sentence", "continuation_marker", "possible_attachment", "layout_signature",
        "visual_description", "transcription", "extraction_confidence",
    ],
)

BOUNDARY_ITEM = {
    "type": "object",
    "properties": {
        "right_page": {"type": "integer"},
        "starts_new_document": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["right_page", "starts_new_document", "confidence", "reason"],
    "additionalProperties": False,
}
BOUNDARY_SCHEMA = _schema(
    "archive_boundaries", {"boundaries": {"type": "array", "items": BOUNDARY_ITEM}}, ["boundaries"]
)

LABEL_ITEM = {
    "type": "object",
    "properties": {
        "group": {"type": "integer"},
        "label": {"type": "string"},
        "document_type": {"type": "string", "enum": PAGE_TYPES},
        "summary": {"type": "string"},
        "date": {"type": "string"},
        "places": {"type": "array", "items": {"type": "string"}},
        "subjects": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["group", "label", "document_type", "summary", "date", "places", "subjects", "confidence"],
    "additionalProperties": False,
}
LABEL_SCHEMA = _schema("archive_labels", {"groups": {"type": "array", "items": LABEL_ITEM}}, ["groups"])


class ArchiveAI:
    def __init__(self, model: str) -> None:
        self.client = OpenAI(max_retries=5, timeout=120.0)
        self.model = model

    def _structured(self, prompt: str, schema: dict[str, Any], image_url: str | None = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        if image_url:
            content.append({"type": "input_image", "image_url": image_url, "detail": "high"})
        response = self.client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": content}],
            text={"format": schema},
            store=False,
        )
        return json.loads(response.output_text)

    def analyze_page(self, path: Path) -> dict[str, Any]:
        return self._structured(PAGE_ANALYSIS_PROMPT, PAGE_SCHEMA, image_data_url(path))

    def decide_boundaries(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._structured(boundary_request_prompt(pages), BOUNDARY_SCHEMA)["boundaries"]

    def review_boundaries(
        self,
        pages: list[dict[str, Any]],
        decisions: dict[int, dict[str, Any]],
        audit_reasons: dict[int, list[str]],
    ) -> list[dict[str, Any]]:
        """Reconsider only splits where structural evidence contradicts the first decision."""
        prompt = boundary_review_request_prompt(pages, decisions, audit_reasons)
        return self._structured(prompt, BOUNDARY_SCHEMA)["boundaries"]

    def label_groups(self, groups: list[list[dict[str, Any]]], offset: int) -> list[dict[str, Any]]:
        numbered = list(enumerate(groups, start=offset))
        return self.label_numbered_groups(numbered)

    def label_numbered_groups(
        self, groups: list[tuple[int, list[dict[str, Any]]]]
    ) -> list[dict[str, Any]]:
        """Label groups whose section numbers need not be contiguous."""
        evidence = []
        for number, group in groups:
            evidence.append({
                "group": number,
                "pages": f'{group[0]["page"]}-{group[-1]["page"]}',
                "page_evidence": [
                    {k: p[k] for k in ("document_type", "title", "summary", "date", "places", "subjects")}
                    for p in group
                ],
            })
        return self._structured(label_prompt(evidence), LABEL_SCHEMA)["groups"]


def boundary_request_prompt(pages: list[dict[str, Any]]) -> str:
    """Build the same request for both model inference and cache fingerprinting."""
    compact = [{k: v for k, v in p.items() if k != "transcription"} for p in pages]
    return boundary_prompt(compact)


def boundary_review_request_prompt(
    pages: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    audit_reasons: dict[int, list[str]],
) -> str:
    by_number = {int(page["page"]): page for page in pages}
    cases = []
    for right_page, reasons in audit_reasons.items():
        left = by_number[right_page - 1]
        right = by_number[right_page]
        case = {
            "right_page": right_page,
            "audit_reasons": reasons,
            "first_decision": decisions[right_page],
            "left_page": _review_page_evidence(left, trailing=True),
            "right_page_evidence": _review_page_evidence(right, trailing=False),
        }
        if right_page - 2 in by_number:
            case["previous_context"] = _review_page_evidence(
                by_number[right_page - 2], trailing=True
            )
        if right_page + 1 in by_number:
            case["next_context"] = _review_page_evidence(
                by_number[right_page + 1], trailing=False
            )
        cases.append(case)
    return boundary_review_prompt(cases)


def image_data_url(path: Path, max_dimension: int = 2200) -> str:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=82, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _review_page_evidence(page: dict[str, Any], *, trailing: bool) -> dict[str, Any]:
    fields = (
        "page", "file", "document_type", "title", "summary", "date", "organization",
        "printed_page_number", "page_role", "header_text", "beginning_text", "ending_text",
        "starts_mid_sentence", "ends_mid_sentence", "layout_signature", "visual_description",
        "extraction_confidence",
    )
    evidence = {field: page.get(field) for field in fields}
    transcription = str(page.get("transcription", ""))
    evidence["transcription_excerpt"] = transcription[-1200:] if trailing else transcription[:1200]
    return evidence
