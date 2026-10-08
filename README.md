# Archival organizer

Developer documentation and the detailed processing pipeline are in [`DEV.md`](DEV.md).

This program examines scans in their physical box order and proposes meaningful document sections such as
“Map of Aarhus harbour”, “Article about the war”, or “Letter from the Civil Defence Agency”. It supports
mixed boxes containing articles, maps, letters, reports, minutes, forms, photographs, drawings, covers,
blank dividers, and other material.

The originals are never moved or renamed. Persistent archive state is stored in `archive.sqlite3`, and a
reviewable `sections.csv` export is produced. Low-confidence decisions are marked `needs_review=yes`.

The page images are sent to the configured OpenAI model. For confidential archives, confirm that this is
permitted by your organization before running the program; a fully local vision model can be substituted
later behind the same three analysis stages.

## How it works

1. A vision model examines and transcribes each scan, recording type, title, date, page role, identifiers,
   subjects, places, layout, headers, footers, and continuation clues.
2. A second pass decides whether every page continues the preceding page. It uses order, consecutive page
   numbers, sentence continuation, typography, headings, dates, and attachments such as maps.
3. Contradictory splits—such as two halves of a sentence being placed in separate documents—receive a
   focused second review with wider text around the page edge.
4. The resulting page groups receive short, specific human-readable labels.

Keeping boundary detection separate from labelling is important: two nearby documents about Aarhus should
remain separate even though their subject is similar.

## Run it

Requirements: Python 3.12+ and an OpenAI API key.

```bash
uv sync
export OPENAI_API_KEY="your-key"
uv run archival-organizer analyze input/sample-scans-img --output output/sample-scans
```

Database schemas from version 4 onward upgrade automatically when starting analysis, relabelling, or the
browser. Upgrades preserve existing page analyses, completed runs, and review edits, and roll back if an
upgrade fails. Archives older than version 4 still require a fresh output directory; archives from newer
application versions are rejected until you update the application.

The input may also be a parent directory containing any number of nested folders. Every folder that
directly contains supported images is treated as a separate collection and processed independently:

```bash
uv run archival-organizer analyze /mnt/e/ocr/scans --output archive-output
```

Nested collections are kept isolated as records in the output's `archive.sqlite3` database. A folder's
images are never combined with images in its parent or children.

To build one shared archive a collection at a time, keep the same input root and output directory:

```bash
uv run archival-organizer analyze /mnt/e/ocr/scans --output archive-output --collection dir1
uv run archival-organizer analyze /mnt/e/ocr/scans --output archive-output --collection dir2
```

For `analyze`, `--collection` selects a folder relative to the input root, such as `dir1` or `box/folder`,
including all image-containing collections beneath it. The selected folder need not contain images itself.
An image-containing parent and its children are still analyzed separately. Repeat the option to select
several folders; overlapping selections are processed only once. Paths match whole directory names, so
`dir1` does not select `dir10`. Unknown or image-free subtrees fail before analysis starts. Use `.` or omit
the option to process all collections, reusing their existing caches. Unselected collections are untouched.
Keep the input root unchanged: passing `scans/dir1` directly instead changes how collection IDs are
assigned and is not a way to select that collection in the shared archive.

The `--limit` value applies separately to each collection. `--refresh-pages` remains a single-collection
operation because page numbers are local to a collection. Select a leaf folder, for example
`--collection dir1/part1 --refresh-pages 38`, or any subtree containing exactly one collection.

For example, this processes up to 20 pages in **each** collection beneath `91+01011-3`:

```bash
uv run archival-organizer analyze /mnt/data2/melica-png \
  --output /home/azks430/melica-output --collection '91+01011-3' --limit 20
```

Uncached page analysis uses four concurrent API requests by default. Adjust this for local resources or API
rate limits with `--workers N`; use `--workers 1` for sequential processing. Cached pages do not occupy a
worker.

Start with a small sample before processing the whole box:

```bash
uv run archival-organizer analyze /mnt/e/ocr/cf-referater-img --output sample-output-llm --limit 20
```

A normal analysis command continues unfinished work and **skips completed collections whose source
images have not changed**. Skipped collections keep their run, extractions, grouping, labels, review
state, and exports. Changing `--model`, `ARCHIVAL_MODEL`, prompts, or batch settings alone does not
reopen them. If all selected collections are complete, no API key or model client is needed.

The archive records an ordered source manifest with SHA-256 image fingerprints. Adding, removing,
reordering, or changing scans updates that collection. Unchanged page extractions are reused by file
identity and content, even when their position or the selected model changes. The new model is used for
new requests; existing results retain their provenance. The default is `gpt-6-luna`.

Page extractions, boundary requests, focused reviews, and document labels are saved as work succeeds.
Interrupted runs reuse that work, including completed label batches. An update only becomes the latest
completed result after all stages succeed and its source manifest is checked again. Its previous completed
analysis and review records remain available if the update fails. Source files themselves are not backed up.

Boundary reuse depends on request evidence, schema, prompt, and batch context; a changed page can affect
its surrounding batch and focused review. Labels are reused for documents with the same ordered content,
independently of their absolute page numbers. Changing one collection does not reopen other completed
collections. Stage records retain the model that actually produced each reused result.

Existing version 4 or 5 archives upgrade transactionally to version 6. On the first analysis command,
completed collections with the same ordered filenames gain fingerprints of their current images and
are skipped, without any model requests. This establishes a baseline under the assumption that their
images have not changed since analysis; it cannot detect replacements made before fingerprints existed.
Use `--refresh-pages` for any known earlier replacements. The version 4 baseline schema remains immutable;
archives older than version 4 still require a fresh output directory.

To retry OCR for selected pages while retaining the rest of the cache, pass individual page numbers or
inclusive ranges. This explicitly reopens the selected collection. Changed evidence invalidates affected
boundary requests and document labels; unrelated results are reused:

```bash
uv run archival-organizer analyze /mnt/e/ocr/p-3-0 --output sample-output-p-3-0 --refresh-pages 38
uv run archival-organizer analyze /mnt/e/ocr/p-3-0 --output sample-output-p-3-0 --refresh-pages 38,42-44
```

To force fresh boundary decisions and focused reviews while retaining cached page analyses:

```bash
uv run archival-organizer analyze /mnt/e/ocr/p-3-0 --output sample-output-p-3-0 --refresh-boundaries
```

To regenerate document labels without redoing extraction or grouping, use `--refresh-labels`.
To redo every stage, use `--reanalyze`. Combine either option with `--collection` to constrain its scope:

```bash
uv run archival-organizer analyze /path/to/scans --output output/test --collection box/folder --refresh-labels --model MODEL
uv run archival-organizer analyze /path/to/scans --output output/test --collection box/folder --reanalyze --model MODEL
```

Selecting a collection alone does not force a rerun. `--refresh-labels` keeps extraction and grouping
when sources are unchanged; changed sources still require the normal dependent updates. These analysis
options regenerate AI document labels; use `relabel` for metadata of manually adjusted groups.

Human boundary corrections remain authoritative when their page pair is still adjacent. Manual page
order is preserved; if the source set changes, new pages follow the existing reviewed order and missing
pages are omitted. The browser and command output flag ordering and boundary corrections that need
attention. Reviewed labels are preserved and marked for review when their page extraction changes.

Progress is printed as the run proceeds. Each page reports its model, detected type, title, transcription
character count, and elapsed time. Boundary and labelling batches also report when they complete; cached
pages, boundary requests, and document labels are identified explicitly. Each collection reports whether
it is skipped, starting, resuming, updating changed sources, or explicitly reanalyzing.

If any page fails, the command exits unsuccessfully before grouping, labelling, or writing final results
for that collection. Other pages finish and successful analyses are cached. The final error summary lists
every failed filename and reason, plus counts of successful/cached and unattempted pages. Authentication
or permission errors stop new page requests; requests already running finish and their successes are saved.
Fix the reported errors and rerun without refresh flags to resume the saved request, including an
interrupted explicit refresh. Supplying refresh flags again deliberately starts another refresh. Later
collections are not processed after a failure. Blank pages, empty transcriptions, and low extraction
confidence do not themselves cause failure.

The input filenames must reflect the physical order of the box. Numeric names are sorted naturally, so
`scan_9.png` precedes `scan_10.png`. This version assumes pages belonging to one document are adjacent. If
the box has been shuffled, the problem needs an additional global page-matching stage before grouping.

## Output

- `archive.sqlite3`: collections, ordered pages, page analyses, sections, boundary decisions, analysis runs,
  reviewer overrides, regenerated section metadata, and undo history.
- `sections.csv`: compact review export with page ranges, labels, types, places, confidence, and review flag.
- `reviewed-sections.csv`: updated export written after a reviewer changes a boundary or regenerates adjusted
  metadata in a single collection. Multi-collection exports include the collection ID in their filenames.

Review the uncertain rows and correct boundaries in the browser before using the results to create
folders or move physical papers. This human review step is especially important for blank backs, unnumbered
appendices, and maps enclosed with reports.

## Preview original images

Browse scans before analysis by passing their source directory and `--preview`:

```bash
uv run archival-browser /mnt/data2/melica-png --preview --port 8002
```

For access from another computer on your network, add `--host 0.0.0.0` and open
`http://<server-address>:8002`. The default host remains `127.0.0.1`.

Preview follows the source folder hierarchy, with readable URLs and breadcrumbs. Each image-containing
folder shows its images in natural filename order, previous/next buttons, keyboard arrow navigation, a
jump-to-image field, and a link to the original. The filename sidebar shows 100 images at a time, with
links to earlier and later batches. Folders may show both their own images and child folders. A visible
Preview label distinguishes this mode from the analysis review interface.

No analysis, API key, or database is required. The filesystem catalog stays in memory, and preview creates
no catalog files, output directories, or disk caches. Startup scans filenames without decoding images;
thumbnails are generated on demand and cached in memory with a 24-entry limit. Restart `archival-browser`
to pick up added or renamed files. Empty branches and symbolic links are omitted. PNG, JPEG, TIFF, and WebP files are supported.
Preview is read-only and has no analysis metadata or grouping controls.

With `--preview`, the positional path is the source directory, so it cannot be combined with `--input`.
Without `--preview`, the path remains the analysis output directory described below.

## Switch between Preview and Analyzed

Pass both your existing analysis output and the source image root to use one browser for both views:

```bash
uv run archival-browser /home/azks430/melica-output \
  --input /mnt/data2/melica-png --host 0.0.0.0 --port 8002
```

Use the **Preview / Analyzed** switch above the viewer or folder list. Preview shows all source images;
Analyzed shows completed results and review controls. Switching keeps the folder and matches the current
image by filename, including when reviewed page order differs from the filesystem order. If a folder or
image has no completed analysis, the Analyzed view explains this and offers a link back to Preview.

The two views use distinct readable URLs: `/preview/91%2B01011-3/part1/` and
`/archive/91%2B01011-3/part1/`. Bookmarks retain the selected view. Restart `archival-browser` to reload source files
and completed analyses. Restarting preserves saved review edits and does not run analysis.

Use the same source root that was passed to `analyze`, or its relocated equivalent. With no `--input`, the
browser retains its analyzed-only view using the stored source locations.
Standalone `--preview` remains available before an analysis database exists. Combined browsing requires
an existing database, but can open one that has no completed analyses yet.

## Browse a completed run

Launch the local review interface with an output directory:

```bash
uv run archival-browser output/sample-scans/ --port 8002
```

Then open <http://127.0.0.1:8002>. The browser shows the source image beside all extracted page information,
the full transcription, the grouping decision, and the section containing the page. Use the section list,
page buttons, arrow buttons, or keyboard arrow keys to move through the run. Page navigation positions
the page strip at the top of the view, above the image and review actions. Click an image to open the
full-resolution original. Collapsible sections remember their open or closed state in this browser,
including when navigating to another page or reloading the browser.

The browser opens at `/archive/` and follows the archive's folder hierarchy. A folder such as
`91+01011-3` appears as a top-level entry even if its images are all in child collections. Click through
folders to reach a collection; folder lists can be filtered by name and show totals for completed
collections beneath each entry. Breadcrumbs such as **Archive Top / 91+01011-3 / part1** link to ancestors.
Page URLs use relative folder paths, for example `/archive/91%2B01011-3/part1/?page=12`; special characters
are URL-encoded while breadcrumbs show the original names. Old collection-ID links redirect to these URLs.

A folder with its own images shows its page viewer together with links to subfolders. If the input root
itself contains an analyzed collection, its pages appear at `/archive/`. The hierarchy is derived from
completed collections: empty and unprocessed folders do not appear. Existing archives need no reanalysis
to use this view; restart `archival-browser` to include newly completed collections. Keep source folder paths
stable, since renaming them changes both readable URLs and the analyzer's path-derived collection IDs.

The section list distinguishes **Label review** (uncertain or stale section metadata) from **Grouping
review** (an uncertain connection between pages inside the section). Both indicators can appear on the same
section. The separate **Adjusted** indicator records that a reviewer changed its page grouping.

The **Correct grouping** card lets a reviewer split a section before the current page, attach a complete
section to its previous or next neighbor, or move the current page after any other page number in the same
collection. A moved page joins the target page's section. Undo restores the most recent boundary or page-order
adjustment. Adjustments never cross a collection boundary and never modify the original AI decision; the
reviewed order, overrides, and undo history are stored separately in the database. Human boundary decisions
carry into the next organizer run when the same two source pages remain adjacent. The browser writes an
updated CSV export after each change. A merged group keeps its first existing section title instead of
concatenating titles. Labels inherited by newly split, merged, or moved sections are marked for review because
they may no longer describe the adjusted groups.

After reorganizing pages, preview and then regenerate changed section metadata from the command line:

```bash
uv run archival-organizer relabel output/sample-scans --dry-run
uv run archival-organizer relabel output/sample-scans
```

`relabel` sends only adjusted groups whose current membership lacks a reviewed label to the labelling model;
it does not repeat page transcription or boundary detection. Use `--collection PATH_OR_ID` to restrict the
operation, or `--all` to regenerate adjusted groups that already have reviewed metadata. The regenerated
label, summary, type, date, places, and subjects are stored against the exact ordered page identities. They
are reused across reruns while that membership remains unchanged and are automatically invalidated when
another split, join, insertion, or reorder changes the group.

The image directory is read from the database. If the originals have moved, pass their new location. For
a recursive result, `--input` is the new parent and each stored relative collection path is appended to it.
Providing `--input` also enables Preview / Analyzed switching:

```bash
uv run archival-browser output/sample-scans/ --input /path/to/scans --port 8002
```
