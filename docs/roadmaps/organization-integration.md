# Organization-system integration roadmap

| Field | Value |
| --- | --- |
| Status | Direction accepted; no implementation started |
| Target | Separate organization project plus optional `HAL` adapter |
| Last reviewed | 2026-08-06 |
| Design | [Organization integration design](../designs/organization-integration.md) |
| Current milestone | Separate project naming and contract definition |

This roadmap tracks delivery status. The separate organization repository should
eventually own its detailed core roadmap; this file should then track only the HAL
adapter and cross-project contract.

## Milestone 0 - Confirm the boundary

- [ ] Select the separate project/package name and approved storage location.
- [ ] Decide whether the first adapter uses an in-process Python API or the CLI JSON
  contract. Prefer the JSON boundary if independent installation is important.
- [ ] Define stable item, task, source-reference, warning, and error schemas.
- [ ] Define work/home profiles without storing credentials or workplace-specific
  details in tracked files.
- [ ] Move the detailed standalone-system requirements into the new project's design
  documents and retain only HAL integration requirements here.

## Milestone 1 - Standalone local-first foundation

- [ ] Implement versioned SQLite migrations and FTS5 capability detection.
- [ ] Implement source configuration, manual scans, hashes, deduplication, changed-file
  detection, ingestion runs, and parser warnings.
- [ ] Implement standard-library parsers for `.eml`, `.txt`, `.md`, `.html`, `.json`,
  and `.csv`; keep PDF/DOCX support optional and report unavailable parsers clearly.
- [ ] Store documents, emails, links, provenance, chunks, tasks, and source relationships.
- [ ] Implement lexical search, metadata filters, manual task management, briefs, and
  Markdown/JSON/CSV exports without requiring an LLM or embeddings.
- [ ] Add synthetic fixtures and offline tests for ingestion, deduplication, FTS,
  filters, links, task transitions, warnings, and exports.

## Milestone 2 - Grounded retrieval and optional intelligence

- [ ] Add a small model protocol and a `HAL` provider adapter without importing HAL
  internals into organization domain modules.
- [ ] Add optional embedding support with graceful fallback to lexical search.
- [ ] Implement explainable hybrid ranking and bounded retrieval packets.
- [ ] Implement grounded question answering with citations and weak-evidence handling.
- [ ] Implement proposed-task extraction with confidence, source excerpts,
  confirmation, dismissal, and duplicate prevention.
- [ ] Validate the approved-software-link scenario end to end using synthetic data.

## Milestone 3 - Read-only HAL integration

- [ ] Add an optional adapter/registration seam to HAL's hardcoded tool registry.
- [ ] Implement `organization_status`, `organization_search`, `organization_show`,
  `tasks_list`, and `daily_brief` as bounded read-only tools.
- [ ] Label retrieved content as untrusted data and prevent source text from becoming
  system, skill, or tool instructions.
- [ ] Add `/find-information`, `/daily-review`, and `/task-review` skills that only
  orchestrate the constrained tools.
- [ ] Verify HAL works unchanged when the package/database is missing or disabled.

## Milestone 4 - Controlled write integration

- [ ] Implement separate tools for ingestion and explicit task state changes.
- [ ] Integrate write tools with HAL's approval/event model after cancellation and
  transcript-invariant work is complete.
- [ ] Add `/email-triage` using proposed tasks rather than automatic confirmation.
- [ ] Add `email_draft_response` and `/draft-email-response`; produce local drafts with
  citations and require review.
- [ ] Keep outbound sending out of scope until a connector-specific security design is
  approved and explicit confirmation is enforced.

## Milestone 5 - Operational hardening

- [ ] Test Windows/PowerShell/VS Code operation with the approved portable Python setup.
- [ ] Add backup, restore, reindex, schema-upgrade, corruption-recovery, and export
  documentation.
- [ ] Add redaction and exclusion tests, payload-size limits, audit-friendly action
  records, and provider failure recovery.
- [ ] Add optional scheduling documentation for PowerShell and Windows Task Scheduler
  without requiring a background service.
- [ ] Reassess PostgreSQL, FastAPI, direct email connectors, and sending only after the
  local CLI MVP is stable.

## Implementation log

No organization-system implementation has started. Research and document
reorganization do not count as implemented product capability.
