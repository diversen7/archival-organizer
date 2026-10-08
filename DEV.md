# Development notes

## Pipeline overview

The organizer treats a box as an ordered sequence of scanned pages. It first describes each page, then
decides where documents begin and end, and finally labels the resulting groups. Separating these tasks
prevents documents about similar subjects from being merged merely because they share keywords.

```text
Ordered image files
        |
        v
OpenAI vision transcription and metadata extraction
        |
        v
Structured page records in archive.sqlite3
        |
        v
Pairwise boundary decisions in overlapping batches
        |
        v
Contiguous page groups
        |
        v
Group labelling and SQLite/CSV output
```

The CLI orchestration is in `archival_organizer/cli.py`, prompt text and builders are in
`archival_organizer/prompts.py`, AI schemas and API calls are in `archival_organizer/ai.py`, database access
is in `archival_organizer/storage.py`, deterministic analysis logic is in `archival_organizer/core.py`, and
human-review regrouping and exports are in `archival_organizer/review.py`.

The analysis browser routes and shared navigation/image helpers are in `archival_organizer/viewer.py`.
Read-only filesystem browsing is in `archival_organizer/preview.py`. Jinja templates live in
`archival_organizer/web/templates/`, with one shared stylesheet and script under
`archival_organizer/web/static/`. Templates contain presentation logic only; URLs, review actions, confidence
labels, and page metadata are prepared in Python before rendering.

## Stage 1: discover pages

`discover_collections()` recursively finds every directory containing PNG, JPEG, TIFF, or WebP files.
Each such directory is an independent collection; images in parent and child folders are never placed in
one page sequence. A direct single-folder input retains the `default` collection ID. Recursive runs use
stable collection IDs and record their source-relative paths in the `collections` table, allowing newly
added folders without remapping existing cached pages.

`analyze --collection PATH` selects a folder and its descendant collections by input-relative path and may
be repeated. Matching uses path components, not string prefixes; image-free parents are valid selectors.
Selection preserves natural discovery order and deduplicates overlapping subtrees. Identity mode
is determined before filtering, so selecting one nested collection keeps the same ID as a full run.
Selectors are validated before creating an AI client or initializing output. `--refresh-pages` is allowed
when exactly one collection remains selected. No schema change is needed for collection selection.
`--limit` applies independently to each selected collection.

Within each collection, `discover_pages()` finds images directly inside that directory. Filenames are
sorted naturally, so `scan_9.png` comes before `scan_10.png`. This order must match the physical order
of the papers in the collection.

The current pipeline assumes all pages belonging to one document are adjacent. A shuffled collection would
need a separate global matching or clustering stage before boundary detection.

## Stage 2: vision transcription and page analysis

Every uncached page is processed by one multimodal model request:

1. The image is rotated according to its EXIF data, converted to RGB, resized to at most 2200 pixels on its
   longest side, and encoded as JPEG.
2. The image is sent to the OpenAI Responses API. The model transcribes the visible text and extracts
   archival metadata in the same structured response.

Independent uncached pages are analyzed concurrently using `--workers` threads (default 4). The shared API
client provides connection pooling; completed records are cached immediately and placed back into physical
page order before boundary detection. Cached pages bypass the worker pool. Lower the worker count if an API
account encounters rate limits; `--workers 1` retains sequential behavior.

The vision model returns strict structured JSON containing:

- document type;
- title and short summary;
- language, date, printed page number, and possible total page count;
- page role: first, middle, last, single, cover, appendix, attachment, or unknown;
- author, sender, recipient, organization, and reference number;
- places and subjects;
- header, footer, and section heading;
- short beginning and ending text fragments;
- whether the body starts or ends mid-sentence, plus explicit continuation markers;
- a repeatable layout signature and possible-attachment flag;
- a visual description for material with little or no text;
- a full best-effort transcription and extraction confidence.

Supported document types currently include articles, invoices, maps, letters, reports, minutes, forms, brochures,
newspapers, photographs, drawings, book pages, covers/dividers, envelopes, notes, blank pages, and `other`.
The type list is deliberately broad: it assists later reasoning but does not by itself determine grouping.

Each source image has a stable row in `pages`; its mutable position is stored separately in `page_order`.
Every completed model response is immediately committed as an immutable `page_analyses` row. A restarted run
can reuse an analysis by page identity and a SHA-256 fingerprint of the page prompt and response schema, so
prompt or schema changes trigger reanalysis automatically. `run_pages` records the exact analysis and order
used by each run. Mixed-model analyses retain their provenance; new requests use the selected model.

Every run also records immutable versions of the page-analysis, boundary, boundary-review, and label prompts.
Identical prompt text is deduplicated in `prompt_versions`, while `run_prompts` preserves the exact versions
used for reproducibility and future prompt editing.

`metadata.schema_version` is upgraded with simple numbered SQL migrations in `archival_organizer/migrations.py`.
Version 4 is the immutable baseline; version 5 adds `boundary_cache`. New archives create that baseline and
apply the same upgrades as existing archives. Analysis, relabelling, and analysis browser startup call
`initialize()`; filesystem preview never opens or initializes a database.
An explicit `BEGIN IMMEDIATE` serializes initializers and keeps all pending upgrades and version updates in
one transaction. Any failure rolls back the whole upgrade. Migrations execute individual statements rather
than `executescript()`, which would implicitly commit the transaction. Add future upgrades under their source
version and increment `SCHEMA_VERSION`; do not alter the baseline or completed migration definitions. Tests
must cover data preservation, repeated initialization, and rollback. Versions below 4 still require a fresh
output directory. Newer, unversioned, and invalid-version databases are rejected without schema/data changes.

## Stage 3: boundary detection

The system evaluates each transition from page N to page N+1. The model decides whether the right-hand page
starts a new document and returns a confidence and short reason.

Evidence for continuing the same document includes consecutive printed page numbers, sentence continuation,
matching headers, identifiers, typography and layout, compatible page roles, a shared title/date, or an
explicitly related appendix, illustration, or map. Evidence for a new document includes a new heading, topic
or document-type change, reset page number, salutation, cover, or blank divider. The full transcription is
kept in the page analysis record but omitted from boundary batches; targeted text fragments and metadata provide the
useful evidence without unnecessarily enlarging the second-pass prompt.

Page records are sent in batches controlled by `--boundary-batch` (default 24 transitions). Each batch also
includes one page of left context, so every transition is evaluated exactly once. If the model omits a
requested transition, the safe fallback starts a new document with confidence `0.0`; this avoids accidental
merging and guarantees that the row is marked for review.

Initial boundary requests and focused reviews use `boundary_cache`, keyed by a SHA-256 fingerprint of the
exact rendered request, selected model, and boundary response schema. Request builders are shared with the
AI methods so fingerprints include the actual evidence and prompt sent to the model. Full window context
and order matter, not just the two pages at a transition. Review requests also include audit reasons, initial
decisions, neighboring context, and transcription excerpts. Initial requests omit full transcription, so a
transcription-only correction invalidates reviews whose excerpts change, not unchanged initial requests.
Responses are committed immediately, even before a run finishes. Cache hits deserialize fresh responses;
fallbacks, structural checks, and confidence caps are applied again on each run, so audit annotations do not
accumulate. `--refresh-boundaries` bypasses both caches and replaces matching entries after successful
requests. Completed run boundaries and human overrides remain separate from this request cache. Existing
version 4 decisions are not backfilled: the first run after migration populates the cache without discarding
page analyses.

After the initial pass, deterministic checks identify contradictory splits. Every split across two
mid-sentence fragments is reviewed even when extracted pagination conflicts, because faint or damaged page
numbers are particularly easy to misread. Page 2, consecutive pagination, and an isolated result classified
as a middle page add further evidence. A three-page sequence such as 16, 47, 18 also triggers both adjacent
boundaries because the middle number may be a damaged or misread 17. These cases receive one focused model
review with neighboring context, trailing and leading transcription excerpts, and visual observations. A
colon at the end of an unnumbered first page followed by page 2 and a numbered section is treated as another
strong continuation pattern, even if the first page mentions separately enclosed material. If the reviewer
retains the split, its confidence is capped below the human-review threshold rather than silently accepting a
structurally implausible section.

The complete decisions are saved to the `boundaries` table as pairs of stable left and right page IDs tied to
their analysis run. `group_pages()` then performs the deterministic step of splitting the ordered page list
at every `starts_new_document=true` boundary.

## Stage 4: group labelling

Groups are labelled in batches controlled by `--label-batch` (default 16 groups). The model receives the
structured evidence from every page in a group and produces:

- a concise human-readable label;
- the dominant document type;
- a group summary, date, places, and subjects;
- a label confidence.

Labels should be specific, for example `Map of Aarhus harbour, 1956`, rather than simply `Map`. An attached
map or photograph may remain part of a report while the report stays the dominant group type.

If a label is omitted by the model, the fallback is `Unlabelled document` with confidence `0.0`.

## Stage 5: output and review

Browser navigation derives all ancestor folders from completed collections' stored relative paths.
`/archive/` is the root and `/archive/{path}/` resolves a recorded collection or a derived folder, never an
arbitrary filesystem path. Child folders are naturally sorted and show aggregate page/collection counts.
Folders containing images and child collections render the page viewer with child navigation. Shared
template macros render breadcrumbs and folder entries; links encode paths while labels retain their
original text. The analyzed hierarchy includes completed collections. Restarting the browser reloads the catalog.

Collection-ID page routes redirect to readable URLs, preserving the selected page. Review and thumbnail
endpoints retain internal IDs, while navigation, original-image links, and post-review redirects use
readable paths. A single in-memory collection map is updated after review operations; the navigation
hierarchy uses the same records. No persisted schema change is required.

`archival-browser SOURCE --preview` builds a separate FastAPI app with browsing/image GET routes
and shared static assets. `load_preview_catalog()` performs one
`os.walk` over the source, reusing supported
suffixes and natural sorting from `core.py`. It records filename tuples by relative directory and ancestor
paths, without decoding images or writing anything. Symlinks are excluded; scan errors are reported at
startup. Image requests resolve only cataloged entries and recheck that the file stays beneath the source
root, including if a directory was replaced by a symlink after startup. Missing or unreadable images return
404. The catalog is an in-memory snapshot, loaded at startup and discarded when the process exits.

Both modes use shared folder navigation, breadcrumbs, image response helpers, the bounded 24-entry thumbnail
cache, and the image-viewer template macro. Preview has its own template without review forms or metadata.
The shared base template adds `?v=PROJECT_VERSION` to CSS and JavaScript URLs. The version is read at
startup from `pyproject.toml` in a source checkout, or package metadata in an installed distribution.
Bump the project version and restart the browser server when releasing asset changes; clients receive
the new asset URLs on their next page load. Edits without a version bump keep the same asset URLs.
Its image list renders at most 100 filenames around the current 1-based page number, with batch links and a
direct page-number jump. Thumbnail work is deferred until an image request. `--preview` and `--input` are
mutually exclusive; the positional argument's meaning is explicit in CLI help. Tests cover discovery without
decoding, no persisted writes/database access, pagination, serving, path validation, CLI dispatch, and normal
archive browsing after the shared-code refactor.

With `archival-browser OUTPUT --input SOURCE`, `create_app()` combines the analysis and filesystem catalogs.
Analysis remains under `/archive/`; preview uses `/preview/`, `/preview-images/`, and `/preview-original/`.
Distinct image routes keep source positions separate from reviewed positions. Mode links carry a URL-encoded
`file` query parameter, resolved against the target catalog's current order. Unanalyzed folders and images
render a clear empty state; matching never substitutes an unrelated image at the same page number.
Navigation and templates are shared with standalone preview. The database must exist, but an explicitly
configured source permits a database with no completed collections yet.

Catalogs are loaded at startup. Restart the browser to discover new source files and completed analyses.
There is no archive-refresh endpoint. A lock serializes review mutations. Thumbnail responses require
browser revalidation so changed page ordering or replaced files do not reuse stale images.
Image lists and catalogs remain in memory.

No source scan is moved, renamed, or modified. The program writes:

- `archive.sqlite3`: the complete catalog, cache, analysis results, and editable review state;
- `sections.csv` (or collection-specific CSV files): compact page ranges, labels, types, places,
  confidence, and `needs_review`.

For a recursive run, all collections share one database. Page-analysis rows are committed as their model
requests finish, which keeps interrupted work resumable. A completed run is an immutable snapshot of page
order, selected page analyses, boundaries, documents, and prompts. The browser continues showing the latest
completed run if a newer run is interrupted. SQLite transactions protect related boundary, document, and
review changes and provide the foundation for later ordering, metadata editing, and cross-collection search.

The browser adds editable review state without replacing the original AI boundary rows. Human decisions
use `boundary_overrides`, `review_page_order`, and `review_history`. Effective boundaries replace overridden
decisions with human confidence and are regrouped deterministically. Page moves persist a separate reviewed
order and attach the moved page to the target page's group. After every edit, the browser writes an
updated reviewed CSV export. A split or merged group retains the first existing section label rather than
concatenating titles, but its label confidence is zero and it remains marked for label review. Unchanged groups
retain their original metadata. Overrides are keyed by the stable left/right page identities rather than an
analysis run, so they apply after a rerun whenever those pages remain adjacent.

`reviewed_labels` stores regenerated metadata by collection and the exact ordered stable page IDs in the
reviewed group. The `relabel` command batch-labels only adjusted groups that do not already have metadata for
their current membership by default. Exact matches survive reruns; membership changes naturally miss the
stored key and return to label review without rerunning page analysis or boundary detection.

The CSV confidence is the minimum of the label confidence and the relevant boundary confidences. A value
below `0.75` produces `needs_review=yes`. Review remains necessary for ambiguous blank pages, missing page
numbers, detached maps, appendices, and damaged or low-contrast scans.

## Model and API behavior

The model defaults to `gpt-6-luna`. It can be changed with `--model MODEL` or the `ARCHIVAL_MODEL`
environment variable. One model currently handles page analysis, boundary detection, and labelling.

The OpenAI client uses strict JSON schemas, a 120-second request timeout, five automatic retries, and
`store=False`. The page image leaves the local machine and is sent to OpenAI. Confirm that this is acceptable
before processing confidential material.

For `P` pages, boundary batch size `B`, and `G` resulting groups with label batch size `L`, a fresh run makes
approximately (plus one request when contradictory splits require review):

```text
P + ceil((P - 1) / B) + ceil(G / L)
```

model requests. Page requests dominate cost and runtime. Page and boundary caching avoid repeating completed
requests after an interruption; label requests are still repeated when a run restarts.

## Progress reporting

Progress messages are flushed immediately. An uncached page reports the selected model, final page
type/title, transcription character count, and total page time. Cache hits, boundary batches, label batches,
and total runtime are also shown.

## Development and verification

Install dependencies and run tests with:

```bash
uv sync
uv run pytest -q
```

For development, use a small, separate output directory so cached production data is not mixed with test
results:

```bash
uv run archival-organizer analyze /path/to/scans --output dev-output --limit 20
```

The deterministic core can be tested without an API key. An end-to-end run requires `OPENAI_API_KEY` and a
model that supports image input and Structured Outputs.

## Example run

```bash
uv run archival-organizer analyze /mnt/e/ocr/cf-referater-img --output sample-output-llm --limit 100
```
