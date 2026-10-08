# Project guidance

This repository is a prototype. Use simple, numbered SQLite migrations for persisted schema changes from
version 4 onward. Increment the schema version and add the upgrade in `archival_organizer/migrations.py`;
keep the version 4 baseline immutable. Preserve existing analyses and review state, apply upgrades
transactionally, and test rollback and data preservation. Versions earlier than 4 still require a fresh
output directory. Keep tests and documentation aligned with this workflow.
