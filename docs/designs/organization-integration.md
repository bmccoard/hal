# Organization-system integration design

| Field | Value |
| --- | --- |
| Status | Direction accepted; implementation not started |
| Target | `neo-py` integration layer |
| Last reviewed | 2026-08-06 |
| Related roadmap | [Organization integration roadmap](../roadmaps/organization-integration.md) |

## Summary

Build the personal/work organization system as a separate local-first Python
application and expose a narrow optional integration to Neo. The organization core
owns ingestion, parsing, SQLite, search, tasks, provenance, briefs, and exports. Neo
provides conversational orchestration through constrained tools and skills.

This keeps the organization system useful without an LLM or Neo and prevents a
domain-specific database application from becoming part of Neo's agent core.

## Problem

Useful information and action items are scattered across email, attachments,
meeting notes, documents, synchronized folders, links, and manual task lists. The
system needs reliable local ingestion, search, task tracking, and source-grounded
answers while remaining usable in a restricted Windows environment.

## Decision record

### Decision

Use a separate implementation boundary with multiple optional front ends:

```text
Standalone organization CLI ----+
Neo organization adapter -------+--> organization core --> SQLite / files
Future approved API ------------+
```

The initial organization system remains CLI-first and requires no background server.
A future home deployment may add PostgreSQL, FastAPI, or direct email connectors
without changing the core domain contracts.

This is a separate project in implementation and lifecycle, but it does not need to
feel like a disconnected product. Neo may present organization capabilities in the
same conversational interface by calling the separate project's stable API or JSON
CLI contract.

The dependency must point toward the organization contract:

```text
organization core <--- standalone CLI
       ^
       +------------- Neo adapter <--- Neo tools and skills
```

The organization core must not import Neo, depend on Neo sessions, or require Neo's
agent loop. The optional adapter may depend on both sides.

### Why this boundary

- Neo is a general coding agent; the organization system is a persistent information
  and task-management application with a separate data lifecycle.
- Ingestion, migrations, parsing, indexing, and task transitions must remain reliable
  without an LLM making decisions.
- The organization system should still work when Neo, embeddings, or a model endpoint
  is unavailable.
- Email and documents are untrusted input. Narrow integration tools provide a safer
  boundary than giving the agent direct database or connector access.
- A separate core can support the restricted work CLI and a future home API without
  forcing server or database dependencies into Neo.

### Consequences

- Two packages or executables may need to be installed and versioned.
- Their Python or JSON contract needs compatibility tests and explicit versioning.
- Neo gains a small optional adapter rather than owning organization migrations and
  dependencies.
- Users can operate the organization CLI directly or access the same data through Neo.
- Work-specific configuration and data remain outside the Neo repository and sessions.

## Ownership boundary

### Belongs in the separate organization project

- Source-folder configuration and discovery, including Power Automate drop folders
  and SharePoint-synchronized directories.
- Email and document parser registry, attachment association, normalization, hashing,
  deduplication, change detection, and parser warnings.
- SQLite migrations, FTS5, optional embeddings, hybrid ranking, and retrieval packets.
- Durable documents, email metadata, links, people, tags, tasks, task-source
  relationships, ingestion runs, and exports.
- Manual task transitions and proposed-task confirmation/dismissal rules.
- Grounded answer assembly, citation formatting, daily briefs, and report exports.
- Connector-specific behavior for direct email access or any future outbound action.
- Backup, restore, reindexing, retention, and work/home data separation.

### Belongs in Neo

- Optional registration of constrained organization tools.
- Conversational skills that decide which organization tools to call and how to
  present the result.
- Visible, temporary workflow progress for operations such as scan, retrieve, review,
  and draft.
- Interactive approval and review surfaces for task changes, ingestion, drafts, and
  any future external side effect.
- The adapter that maps Neo's provider interface to the small model protocol exposed
  by the organization project, when an organization operation needs an LLM.
- Graceful behavior when the optional organization package, database, parser, or
  embedding capability is unavailable.

### Must not be represented as a skill or Neo workflow

- SQLite records, durable task status, ingestion history, and source provenance.
- File/email parsing, hashing, deduplication, FTS indexing, or embeddings.
- Authorization to send email or modify an external system.
- Background scheduling or folder monitoring.

A skill is prompt guidance, not executable or durable behavior. Neo's workflow is a
temporary visual checklist, not the organization system's task database or scheduler.

## Goals

- Keep indexed content and exports local by default.
- Work with manually scanned folders and SQLite in restricted Windows environments.
- Ingest repeatedly without duplicating documents, tasks, chunks, or embeddings.
- Search lexically without requiring embeddings and optionally add hybrid retrieval.
- Produce grounded answers with source paths and available message/document metadata.
- Track manual tasks and review proposed tasks extracted from source material.
- Integrate with Neo through narrow, testable capabilities rather than unrestricted
  database or shell access.

## Non-goals

- Do not embed the organization database, migrations, or parser framework in Neo's
  core agent loop.
- Do not require a web server, Docker, Redis, a message broker, or a file watcher.
- Do not treat Neo workflow checklist state as durable task storage.
- Do not require embeddings for ingestion or lexical search.
- Do not send email in the initial integration.
- Do not build an enterprise records-management platform in the MVP.

## Requirements

### Core boundary

- **ORG-001:** The organization core must run independently of Neo and expose a
  stable Python API plus machine-readable CLI output.
- **ORG-002:** SQLite and FTS5 must provide the default storage and lexical-search
  foundation; optional services must not be startup requirements.
- **ORG-003:** Parser, storage, retrieval, embedding, and model interfaces must remain
  replaceable without coupling domain logic to a specific CLI or provider.
- **ORG-004:** Repeated ingestion must use stable hashes and source identity to detect
  duplicates and changed versions.
- **ORG-005:** Records, links, task candidates, and answers must retain source
  provenance sufficient to locate the original material.

### Neo integration

- **ORG-INT-001:** Neo must access organization data through constrained named tools,
  not direct SQL generated by the model.
- **ORG-INT-002:** Read operations and write operations must be separate capabilities
  so approvals and future subagent restrictions can distinguish them.
- **ORG-INT-003:** Skills may orchestrate tools but must not be the implementation of
  ingestion, search, task transitions, or email drafting.
- **ORG-INT-004:** Retrieved excerpts must be bounded and clearly labeled as untrusted
  source content, never interpreted as system or skill instructions.
- **ORG-INT-005:** Model requests must contain only focused retrieved excerpts and
  approved metadata, not an entire archive or inbox.
- **ORG-INT-006:** Every answer must cite its evidence or explicitly report that no
  reliable answer was found.
- **ORG-INT-007:** Email response support must create a reviewable draft first. Actual
  sending requires a separately configured connector and explicit user approval.
- **ORG-INT-008:** The integration must remain optional; Neo must start and operate
  normally when the organization package or optional parsers are unavailable.

## Capability allocation

| Capability | Owner | Neo surface |
| --- | --- | --- |
| Folder/email ingestion | Organization core | `organization_ingest` tool |
| Document and email search | Organization core | `organization_search` tool |
| Task queries | Organization core | `tasks_list` tool |
| Task creation/transitions | Organization core | `task_add` / `task_update` tools |
| Daily brief generation | Organization core | `daily_brief` tool and skill |
| Email response drafting | Organization core plus model adapter | `email_draft_response` tool and skill |
| Conversational triage policy | Neo | `/email-triage` skill |
| Visible execution progress | Neo | Optional workflow checklist |
| Durable task/document state | Organization core | Never stored in Neo workflow state |

## Requested command mapping

The conversational command ideas map to deterministic operations as follows:

| User-facing idea | Durable implementation | Neo integration |
| --- | --- | --- |
| `update-emails` | Scan configured email drop folders, ingest changed files, and record warnings in the organization project | Optional `organization_ingest` tool and `/email-triage` or `/update-emails` skill |
| `get-tasks` | Query the organization task store using explicit filters | `tasks_list` read-only tool |
| `list-tasks` | Same task query operation with terminal-oriented presentation | `tasks_list` tool; avoid a duplicate tool contract |
| `respond-to-email` | Retrieve the source message and create a local reviewable draft | `email_draft_response` tool and `/draft-email-response` skill; no implicit sending |

At work, `update-emails` initially means ingesting `.eml` files and available
attachments saved by Power Automate or another approved process. It does not imply
that Neo connects directly to Outlook. Direct connectors remain a later organization-
project extension.

## Initial tool contracts

The first integration should be read-only:

- `organization_status`: database health, last scan, and parser warnings.
- `organization_search`: bounded results with IDs, excerpts, scores, and provenance.
- `organization_show`: retrieve one item by stable ID with bounded content.
- `tasks_list`: filter tasks by status, date, owner, project, or source.
- `daily_brief`: return a generated or deterministic brief with source references.

Write tools should be added only after the read path is stable:

- `organization_ingest`
- `task_add`
- `task_update`
- `task_confirm`
- `task_dismiss`
- `email_draft_response`

## Skill boundary

Useful skills include `/email-triage`, `/daily-review`, `/task-review`,
`/find-information`, and `/draft-email-response`. Each skill defines the sequence and
quality policy for calling organization tools. Skills do not receive direct database
credentials and do not silently send or delete anything.

Skills are appropriate for multi-step policies such as:

1. Search recent messages.
2. Show likely important items with citations.
3. List proposed tasks without confirming them.
4. Ask the user which tasks to accept.
5. Produce a brief or response draft.

The underlying search, task query, confirmation, and drafting operations remain
deterministic tools owned by the organization project.

## Workflow boundary

Neo may show transient progress such as:

```text
completed  Scan configured email folder
completed  Index new and changed messages
active     Review proposed tasks
pending    Prepare daily brief
```

This workflow state exists only to explain the current run. The organization database
is the authority for task status, ingestion state, warnings, and resumability.

## Security and trust

- Treat email and document text as hostile input that may contain prompt injection.
- Keep work and home profiles physically/configurationally separate.
- Use only approved model and embedding endpoints for the selected profile.
- Support exclusions by folder, pattern, sender, or category before indexing.
- Avoid secrets and unnecessary document payloads in logs.
- Preserve provenance through parsing, retrieval, answering, task extraction, and
  export.
- Require explicit confirmation for material external side effects.

## Acceptance criteria

The integration is successful when:

1. The organization CLI works without Neo or a running server.
2. Neo starts normally when organization integration is absent or disabled.
3. Neo can search and cite synthetic indexed email/document content through read-only
   tools without receiving the entire archive.
4. A skill can produce a daily review from deterministic organization-tool results.
5. Proposed tasks retain their source excerpt and require confirmation.
6. Email response support produces a draft and never sends implicitly.
7. Tests use synthetic fixtures and require no network or personal/work data.

## Open decisions

- Final package and executable names (`organization-core`, `neo-organize`, or another
  neutral name).
- Whether the first Neo adapter calls the Python API in process or invokes the
  standalone CLI's JSON interface.
- Which PDF and DOCX parsers are acceptable in the restricted environment.
- Which approved embedding protocol and local vector representation to support.
- Where the separate project will live and how its releases will be installed at work.
