from __future__ import annotations

import json
from typing import Any


# Used for the first-pass analysis that extracts structured metadata and text from one page image.
PAGE_ANALYSIS_PROMPT = """Act as an archival document scanner. Analyze and transcribe this single scanned page.
It may be any document type, including a map or photograph with little text. Return observable evidence
that will help a later model determine whether adjacent pages belong to the same physical document.

Transcribe visible text faithfully in reading order. Preserve paragraph breaks and mark unreadable passages
as [unclear] rather than guessing. Beginning and ending text must be short fragments from the document body,
not headers or page numbers. A layout signature should concisely describe columns, typography, letterhead,
margins, and other repeatable visual structure. Infer page role only when supported by visible evidence.
Use empty strings/lists for unknown values and do not invent facts.

Preserve transcription, visible titles, headings, headers, footers, names, quotations, and text fragments
in their original language and spelling; do not translate them. Write model-generated summaries, layout
and visual descriptions, and other descriptive prose in Danish."""


# Used to decide where documents split by comparing metadata from adjacent pages.
BOUNDARY_PROMPT = """Decide whether each page after the first continues the immediately preceding physical
document or starts a new document. Preserve archival order. A topic change, new title, reset page number,
different layout/type, salutation, cover, or blank divider suggests a boundary. Sentence continuation,
consecutive page numbers, matching identifiers/header/layout, compatible page roles, or an explicitly related
map/appendix suggests a join. Pay particular attention to starts/ends-mid-sentence and beginning/ending text.
Treat dotted numbers as possible hierarchical section identifiers rather than decimal page numbers: a move
such as 2.3 to 3.0 can be normal chapter progression, especially when the institutional masthead and manual
title remain the same. A changing chapter-title cell or a chart replacing prose is not by itself a new
physical document.
Direct grammatical continuation across the page edge is stronger evidence than a soft document-type or
layout difference. A page 2 normally lacks the title and letterhead on its first page. Do not treat an
organization mentioned only in body text, or a standalone handwritten archival number, as decisive evidence
of a new document. If a page number conflicts with an exact sentence continuation, consider whether a faint
or damaged number was misread. Use neighboring pagination to catch OCR errors: a sequence such as 16, 47, 18
may mean the middle 17 was misread. An unnumbered first page ending with a colon followed by page 2 and a
numbered section normally continues the same document. A reference to an attached report does not prove that
the next page begins that attachment; check whether the next text instead continues commentary introduced by
the colon. Do not merge separate documents merely because their topics match. Return one decision
for every right-hand page shown after the first, using its `page` number. Write each decision's `reason`
in Danish, while preserving quoted source text, titles, headings, and names in their original language."""


def boundary_prompt(page_evidence: list[dict[str, Any]]) -> str:
    return f"{BOUNDARY_PROMPT}\nPage evidence:\n{json.dumps(page_evidence, ensure_ascii=False)}"


# Used to reconsider proposed splits that conflict with pagination or text-continuation evidence.
BOUNDARY_REVIEW_PROMPT = """Audit the questionable page splits below. Decide again whether each right page starts
a genuinely new physical document or continues its left page. This is a focused contradiction check, not
a request to defend the first decision.

An uninterrupted sentence across the page edge is exceptionally strong continuation evidence. A numbered
page 2 following an unnumbered titled page is normal. A continuation page normally lacks the title and
letterhead found on its first page, so that difference is not by itself a boundary. Treat report/minutes as
potentially synonymous page classifications, and treat letter/report as soft classifications when a titled
memorandum continues from its cover-like first page. An organization mentioned in body text is not proof of a new
originating organization, and a standalone handwritten number may be an archival annotation rather than
document pagination. Conversely, do not join two unrelated incomplete documents solely because both text
edges are fragments. Treat conflicting pagination as evidence to investigate, not a veto: page numbers are
often faint, damaged, handwritten, or misread by OCR. When the excerpts form an exact grammatical sentence,
consider whether the reported number is wrong. Resolve the actual language across the seam using the
transcription excerpts and visual observations. Also use the surrounding context: pagination such as
16, 47, 18 is strong evidence that the middle number may actually be a misread 17, provided document style
and content are compatible. An unnumbered first page ending with a colon followed by page 2 and a numbered
section strongly suggests continuation. Do not assume that page 2 starts an attachment merely because page 1
mentions enclosures; determine whether page 2 continues the commentary introduced immediately before it.
Likewise, dotted identifiers may encode a manual's hierarchy: X.Y followed by X+1.0 is chapter progression,
not a numbering reset, when stable masthead cells identify the same organization and overarching document.
A changing chapter-title cell or body layout (for example, prose followed by an organization chart) is then
only an internal section change.

Return exactly one decision for every `right_page` in the cases. Write each decision's `reason` in Danish,
while preserving quoted source text, titles, headings, and names in their original language."""


def boundary_review_prompt(cases: list[dict[str, Any]]) -> str:
    return f"{BOUNDARY_REVIEW_PROMPT}\nCases:\n{json.dumps(cases, ensure_ascii=False)}"


# Used after grouping to create descriptive labels and summary metadata for each document.
LABEL_PROMPT = """Create a concise, specific, human-readable archive label for each physical document.
Prefer labels such as 'Kort over Aarhus havn, 1956' or 'Artikel om civilforsvarsrum' rather than
generic names. Do not invent facts. Choose the dominant document type; a document may contain an attached
map or illustration. Write generated labels, summaries, and subjects
in Danish, while preserving source titles, quotations, proper names, and official organization names in their
original language and spelling."""


def label_prompt(evidence: list[dict[str, Any]]) -> str:
    return f"{LABEL_PROMPT}\nEvidence:\n{json.dumps(evidence, ensure_ascii=False)}"
