# No speculative persistence in ops-crm

ops-crm's SQLite layer declared `interactions`, `runs`, `notes`, and `decisions` tables (plus a `schema_migrations` version stamp) that no code ever wrote to or read from. Deleted 2026-07-09: a table without an insert path isn't a feature, and an empty schema constrains nothing. Tables get added when their first insert path lands — don't re-add these speculatively.

`interactions` is the deliberate one: it maps to the outreach tracking spec in the business design doc (first response time, follow-ups sent, audit log — see `docs/mmtvu-ai-automation-holding-company-design-2026-07-08.md`). When outreach logging starts, re-add it designed against that spec rather than resurrecting the old speculative shape (~10 minutes of work).

Schema migration is handled by direct `PRAGMA table_info` checks in `init_db` (as the P0.6 archival-column migration does), not by a version table.
