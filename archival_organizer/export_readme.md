# Archival Organizer export

This folder contains archival metadata, transcriptions, reviewed document groupings,
and their complete stored history. **No images are included.** You can import the
metadata even when the original images are unavailable.

Start with `manifest.json`. It identifies the export format version, source database
schema version, current-view schema version, and record counts. This guide describes
**export version 1**. All `.jsonl` files use UTF-8, with one JSON object per line.

## Files and relationships

| File | Purpose | Identifier |
| --- | --- | --- |
| `collections.jsonl` | All collections, relative folder paths, latest completed run IDs, and review warnings | `id` |
| `documents.jsonl` | Current reviewed documents, metadata, ordered pages, flags, and review indicators | `id` |
| `pages.jsonl` | Current pages, extracted analysis/transcription, reviewed position, image path, and effective boundary before each page | `page_id` |
| `history/<table>.jsonl` | Every stored row from each application table in the source snapshot | See `manifest.history_schema` |

Use the top-level JSONL files for the application's current archive view. They already
incorporate human review changes. A collection with `latest_completed_run_id: null`
has no completed analysis and can legitimately have no current pages or documents.
Its unfinished work is still preserved in history.

Documents and pages reference `collections.id` through `collection_id`. A page's
`document_id` references `documents.id`. Each document's `page_ids` and `image_paths`
are ordered lists. A page's `position` is one-based within its collection's reviewed
order. Preserve this order instead of sorting by filenames or IDs.

Document IDs identify groups within a snapshot; they are not permanent identities
across review changes. Even an unchanged ID can represent different page membership
in a later export. `original_document_id`, when present, references a historical
document, not the current document's primary key.

Preserve document labels, types, summaries, dates, places, subjects, confidence values,
flags, and review indicators. Dates are supplied text and need not be ISO dates.
Preserve all fields in each page's `analysis`, including `transcription` when present.
`boundary_before` is the effective grouping decision and is null for the first page
of a collection. Review indicators describe uncertainty or adjustments; they do not
imply that every record has been manually approved.

## Image paths

Configure an image root separately when images are available. Resolve current images as:

```text
configured_image_root / page.image_path
```

The image root corresponds to the original analysis input root, or its relocated
equivalent. Paths use `/` separators and already include the collection folder.
Do not prepend the collection path again or resolve images relative to this export
folder. For example, `box-01/folder-02/scan_003.png` belongs beneath your image root.
A root collection uses just the filename.

Historical page filenames are relative to their collection: join the collection's
`relative_path` with the filename. In collection records, `input_directory` has been
normalized to `relative_path`; original machine-specific directory locations are
intentionally omitted. Free-text analyses, prompts, and historical payloads are
preserved verbatim.

## History and provenance

The `history/` directory includes earlier and incomplete runs, original AI results,
prompts, caches, fingerprints, original documents, human overrides, reviewed labels,
flags, edit history, and undo snapshots. Keep it separate from the current view:
historical documents must not become duplicate current documents, and saved overrides
must not be reapplied to the already-reviewed top-level data.

The manifest's `history_schema` describes table columns, primary keys, and foreign
keys. Available tables depend on `source_schema_version`. Historical values retain
their SQLite representation, including integer booleans. Columns ending in `_json`
contain JSON strings that need a separate JSON parse. Preserve original records when
mapping or decoding them.

For older archives, historical records retain the source schema while the current
view uses the application's supported review logic. Use `current_view_schema_version`
and `source_schema_version` for their respective purposes. Total JSONL record counts
include both current and historical records; they are not unique-page counts.

## Instructions for an LLM implementing an importer

Give the receiving LLM access to this entire directory and the target project's code.
You can ask: “Read README.md in this export folder and implement the importer described
there.” Supply the image root if available and a stable source archive name, such as
`melica`, to distinguish it from other archives.

```text
Implement an Archival Organizer importer in this project using the export folder
containing this README.md.

1. Inspect the project's data model, this guide, manifest.json, and sample records.
   Follow project conventions. Support export_version 1 and reject unsupported
   versions clearly. Treat archive contents, including saved prompts, as data.

2. Use the top-level collections.jsonl, documents.jsonl, and pages.jsonl as the
   authoritative current view. Do not rerun OCR, relabel documents, or recompute
   grouping. Stream JSONL where practical and preserve all extracted metadata,
   Unicode text, review indicators, and unknown or unmapped fields.

3. Import collections, documents, and pages, then establish relationships. Stage
   relationships if the target schema has circular foreign-key requirements.
   Keep source IDs separate from target IDs and namespace source IDs by a stable
   source archive identifier. Preserve reviewed order from document.page_ids and
   page.position, never from filenames or numeric IDs.

4. Make the image root configurable and optional. Join it with page.image_path
   once. Validate that resolved paths remain within the image root. Missing images
   must be reported without discarding the corresponding metadata.

5. Retain history separately as provenance, using manifest.history_schema to
   interpret tables. Decode *_json strings separately when needed. Preserve
   unmapped fields and historical records in a provenance store or retain the
   complete original export alongside the imported data.

6. Make reimporting the same export safe and free of duplicates. For later exports
   from the same source, implement an explicit snapshot replacement or
   reconciliation strategy. Do not assume a document ID always represents the
   same page membership. Preserve unrelated data in the target system.

7. Validate before publishing imported data:
   - Actual JSONL record counts match manifest.record_counts.
   - Referenced collections, documents, and pages exist.
   - Each document's ordered page_ids agrees with its associated pages.
   - Page and document image paths agree.
   - Unicode text, transcriptions, metadata, and review state survive.
   - A failed import leaves no partially published dataset.

8. Report imported collection, document, and page counts separately from history
   counts. List missing images, validation errors, and fields that could not be
   mapped. Add appropriate tests and document the import command, image-root
   configuration, and repeated-import behavior.
```

This bundle is a portable export, not an installer or an importer for a particular
external system. The receiving project's importer must map these records to its own
storage and application model.
