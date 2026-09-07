# Migration versioning policy

Migrations are plain Python modules here with an `upgrade(session)` function.
The runner (`app/migrations/runner.py`) applies them **in full-filename lexical
order** and records the applied state in `schema_migrations.version` keyed by the
**full module filename** (`0007_telegram_links.py`), not just the numeric prefix.

## Rules

1. **Append-only.** Once a file has been applied to any database, never edit or
   delete it — write a new migration instead.
2. **Unique numeric prefix.** Never reuse a sequence number. File name format:
   `NNNN_short_snake_description.py`. The runner warns (does not fail) when two
   files share a prefix; treat that warning as a review blocker.
3. **Idempotent guards.** Column/table additions should check existence first so
   replaying a migration is a no-op rather than an error.

## Known exception (do not copy)

Two additive migrations share the prefix `0003`:

- `0003_video_progress.py` — adds `videos.progress`
- `0003_video_thumbnails.py` — adds `videos.thumbnail_path` + job progress cols

Both are column-guarded and applied in filename order, so they are safe on every
existing database. They were consolidated under one prefix during early
development before the uniqueness rule above was adopted.

A future cleanup may renumber the whole chain **only** via a dedicated migration
that rewrites `schema_migrations` rows for already-applied databases — never by
renaming files that have already shipped.
