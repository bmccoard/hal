# RAG system build design

| Field | Value |
| --- | --- |
| Status | Proposed; usable manually with current HAL, dedicated workflow not implemented |
| Target | A separate RAG application built and operated through HAL |
| Last reviewed | 2026-08-13 |
| Related designs | [Harness](harness.md), [workflow orchestration](designs/workflows.md), [organization integration](designs/organization-integration.md) |

## Summary

HAL should be able to guide the construction of a retrieval-augmented generation
(RAG) system from unfamiliar source data. The operator identifies approved data
locations; HAL inspects representative structure and metadata, reports what it found,
and waits for the operator to approve or revise the proposed design. Implementation
then proceeds as bounded, testable milestones covering ingestion, multimodal
extraction, normalization, indexing, retrieval, grounded answers, evaluation, and a
HAL-facing integration.

The harness and workflow have different roles:

- The **harness** enforces capabilities, budgets, verification, bounded repair, and
  durable outcomes for each run.
- The **workflow** orders discovery, design, implementation, evaluation, and review.
- The **RAG application** owns durable source state, parsing, indexes, provenance,
  evaluation data, and query behavior.
- **HAL** is the conversational build and operating front end. It must not become the
  RAG database, job scheduler, or source of truth.

The system must not "self-improve" by making unbounded production changes. Improvement
means a controlled evaluation loop: propose a candidate, test it against versioned
fixtures and reviewed examples, compare it with the accepted baseline, present the
evidence, and promote it only when configured gates and required approvals pass.

## Current HAL boundary

HAL can support the first version of this process today, with limitations:

- `/design` and `/plan` provide separate read-only analysis turns.
- `/workflow feature <request>` executes `design -> plan -> build -> review` with the
  harness applied to every phase.
- The harness can bound tool and provider calls, run deterministic verification, allow
  bounded repairs, and journal outcomes.
- Glob and grep discovery operate inside the resolved workspace. Other locations must
  be deliberately made available through an approved workspace layout, a deterministic
  source-inventory command, or a constrained extension tool.
- The current workflow runs straight through. It has no built-in approval checkpoint
  between design and build, no durable workflow state, and no automatic resumption.
- Phase handoffs contain bounded final responses rather than the complete earlier tool
  transcript. Durable discovery artifacts therefore belong in the RAG project, not
  only in a phase response.

Until checkpointed workflows exist, the operator should run discovery and planning as
separate commands, approve their outputs, and invoke one feature workflow per approved
milestone. A proposed dedicated `rag-build` workflow is described below; it is not a
claim about current implementation.

## Goals

- Discover the shape, formats, volume, update behavior, and quality of approved data
  locations before selecting a storage or retrieval design.
- Require an operator decision after discovery and before material implementation.
- Preserve source identity and evidence from the original item through extraction,
  chunks, retrieval, and answers.
- Support text, tables, structured files, documents, and images through replaceable
  extractors.
- Use deterministic validation first and model-based extraction only where it adds
  measurable value.
- Support lexical retrieval without embeddings and optional hybrid retrieval when an
  approved embedding endpoint is available.
- Improve extraction and verification through bounded, reproducible evaluations.
- Expose narrow RAG tools to HAL so users can ingest, search, ask, inspect evidence,
  review uncertain records, and submit feedback conversationally.
- Keep the RAG core useful through its own CLI or API when HAL or a model provider is
  unavailable.

## Non-goals

- Do not send every source file to a model or place an entire corpus in model context.
- Do not let prompts silently add data locations, relax exclusions, or authorize
  external services.
- Do not treat HAL sessions or workflow state as the ingestion database.
- Do not allow the model to write arbitrary SQL against the production store.
- Do not continuously rewrite extractors, prompts, schemas, or indexes without a
  versioned evaluation and promotion gate.
- Do not imply that harness policy is an operating-system sandbox.
- Do not require a vector database, background service, or embeddings for the first
  usable system.

## Primary design decision

Build the RAG system as a separate application with a stable CLI or API. Integrate it
with HAL through constrained tools and reusable skills:

```text
Approved sources
      |
      v
RAG source adapters --> content/version store --> extract/normalize
                                                   |
                                                   v
                              lexical index + optional vector index
                                                   |
                                                   v
                                   retrieve/rerank/context packet
                                                   |
                          +------------------------+------------------+
                          |                                           |
                     RAG CLI/API                              HAL adapter tools
                                                                      |
                                                                      v
                                                                 HAL TUI/REPL
```

The dependency direction is one-way: the optional HAL adapter depends on the RAG
contract; the RAG core does not import HAL or depend on HAL sessions.

## Source declaration and discovery

### Private source manifest

Data locations and sensitive endpoint details belong in an ignored local file such as
`rag.sources.local.yaml`, not in this repository or a model prompt. A representative
shape is:

```yaml
sources:
  - id: engineering_docs
    kind: filesystem
    root: D:/approved/engineering
    include: ["**/*.pdf", "**/*.docx", "**/*.xlsx", "**/*.png"]
    exclude: ["**/archive/**", "**/secrets/**"]
    sensitivity: internal
    extraction_profile: engineering

  - id: parts_export
    kind: filesystem
    root: D:/approved/exports
    include: ["parts-*.csv"]
    sensitivity: internal
    extraction_profile: part_catalog
```

The manifest is authoritative. Text found inside a document is untrusted content and
cannot add sources, change policy, or issue instructions to HAL.

### Discovery behavior

Discovery should inspect metadata and bounded samples before reading content broadly.
A deterministic inventory command or extension should collect:

- Source identifier and resolved root.
- File counts, total bytes, size distribution, and modification ranges.
- Extensions, MIME signatures, likely encodings, and container formats.
- Directory patterns and likely logical collections.
- Representative schemas for CSV, JSON, XML, spreadsheets, databases, and exports.
- Document page counts, table/image presence, OCR indicators, and embedded attachment
  counts without uploading content.
- Duplicate and near-duplicate indicators using stable hashes.
- Candidate identifiers, part-number formats, dates, units, relationships, and join
  keys.
- Parse failures, password-protected files, corrupt items, unsupported formats, and
  access errors.
- A bounded, reproducible sample list used for deeper analysis.

Discovery produces durable, reviewable artifacts such as:

```text
artifacts/discovery/source-inventory.json
artifacts/discovery/schema-profiles.json
artifacts/discovery/sample-manifest.json
artifacts/discovery/parser-coverage.json
artifacts/discovery/design-report.md
```

The report must state what was inspected, what was sampled, what was excluded, and
what remains unknown. It must not describe a sample as though every item was read.

### First approval checkpoint

Before implementation, HAL presents:

1. The discovered source inventory and sampling coverage.
2. Proposed canonical record types and relationships.
3. Parser and OCR/vision requirements.
4. Sensitive-data and model-routing implications.
5. Storage and retrieval alternatives with tradeoffs.
6. Open questions, unsupported inputs, and estimated indexing cost.
7. A milestone plan and proposed acceptance metrics.

The operator may approve, edit the source manifest, narrow scope, choose storage or
model constraints, or request more discovery. No ingestion of the complete corpus or
production index build begins before this decision.

## Durable data and provenance model

The exact schema is selected after discovery, but the following concepts should remain
separate:

| Record | Responsibility |
| --- | --- |
| `source` | Approved logical data location and policy profile. |
| `source_item` | Stable identity for a file, record, message, page, or object. |
| `source_version` | Hash, timestamps, size, parser version, and change history. |
| `asset` | Original or derived binary such as an image, attachment, or page render. |
| `extraction` | Versioned text, table, OCR, vision, or structured-parser output. |
| `entity` | Normalized part, person, product, document, or domain object. |
| `entity_evidence` | Source span and extraction supporting an entity value. |
| `chunk` | Retrieval unit with document position, metadata, and provenance. |
| `embedding` | Optional vector tied to a chunk and exact embedding model version. |
| `evaluation_case` | Versioned input, reviewed expectation, and allowed tolerances. |
| `evaluation_run` | Candidate configuration, metrics, failures, and comparison result. |
| `feedback` | User correction or relevance judgment awaiting reviewed incorporation. |
| `query_trace` | Retrieval candidates, scores, context packet, citations, and timing. |

Raw source identity, normalized data, retrieval chunks, and model-produced claims must
not be collapsed into one table. Every answer and extracted field must be traceable to
the source version and location that support it.

Start with a local relational store such as SQLite plus FTS5 unless discovery proves
that concurrency, scale, or deployment requirements justify a service. Keep vector
storage behind an interface so lexical-only and hybrid deployments share the same
domain contracts.

## Ingestion and extraction pipeline

The pipeline should be deterministic and resumable:

```text
scan -> identify -> hash -> deduplicate -> parse -> extract assets
     -> normalize -> validate -> chunk -> index -> record outcome
```

Required properties:

- Idempotent re-ingestion with stable source IDs and content hashes.
- Explicit parser versions and configuration fingerprints.
- Transactional commits or recoverable checkpoints at item boundaries.
- Bounded parser output and structured warnings.
- Quarantine for corrupt, unsupported, or suspicious inputs.
- Exclusion rules applied before content is sent to any model.
- Incremental reindexing of changed items and removal policy for deleted sources.
- A dry-run mode that reports intended work without changing durable state.

PDFs, office documents, HTML, email, tabular data, and structured exports should use
format-specific adapters. A generic language model is not the primary parser.

## Image and multimodal extraction

The system first inventories images and determines whether they contain useful data.
Images may include scanned pages, diagrams, labels, tables, nameplates, screenshots,
or decorative assets. The pipeline should:

1. Extract embedded images and page renders with their source coordinates.
2. Use deterministic metadata, perceptual hashing, orientation correction, and image
   quality checks.
3. Run local or approved OCR where text is likely present.
4. Apply domain-specific parsing to OCR output, such as part-number or measurement
   patterns.
5. Route only eligible, non-excluded images to an approved vision model when OCR and
   deterministic methods are insufficient.
6. Store structured output, confidence, model/parser version, and source coordinates.
7. Send uncertain or conflicting fields to a review queue.

Images must not be uploaded merely because they exist. Routing depends on source
sensitivity, the configured extraction profile, endpoint approval, and a measurable
need. Generated descriptions are claims supported by an image, not replacements for
the original evidence.

## Entity and part-number verification

Part numbers and similar domain identifiers require an explicit verification service,
not prompt-only judgment. Candidate verification may combine:

- Unicode, whitespace, punctuation, case, and vendor-prefix normalization.
- Configured regular expressions and check-digit algorithms.
- Exact lookup against an authoritative catalog or approved export.
- Alias, supersession, revision, and manufacturer relationships.
- Cross-field validation such as manufacturer plus series plus dimensions.
- Agreement across independent source items.
- OCR confusion handling for characters such as `0/O`, `1/I/l`, and `5/S`.
- Confidence calibration and explicit `verified`, `ambiguous`, `rejected`, or
  `unverified` states.

Every accepted value retains the original text and source coordinates. The system must
never convert a plausible model guess into a verified identifier. Ambiguity is exposed
to users through HAL with side-by-side evidence and a correction action.

## Retrieval and answer construction

Retrieval should be explainable and testable independently of answer generation:

1. Parse the query into filters and retrieval terms without discarding the original.
2. Retrieve lexical candidates and, when enabled, vector candidates.
3. Apply metadata access filters before material enters the context packet.
4. Fuse scores with a documented algorithm and optionally rerank a bounded candidate
   set.
5. Diversify by source and collapse duplicates.
6. Assemble a size-bounded context packet with stable source citations.
7. Generate an answer constrained to supplied evidence.
8. Return citations, uncertainty, and an explicit no-answer result when evidence is
   insufficient.

The RAG core should expose retrieval results without requiring an answer model. This
allows deterministic search tests and lets users inspect why an answer was produced.

## Controlled improvement cycle

"Self-improvement" is implemented as bounded candidate evaluation:

```text
reviewed examples -> baseline -> candidate change -> offline evaluation
       ^                                      |
       |                                      v
user corrections <- failure analysis <- compare and gate
```

The cycle applies separately to extraction, entity verification, retrieval, and answer
quality. Each cycle must:

1. Select a versioned evaluation suite that does not expose unrelated production data.
2. Record the current accepted baseline.
3. Propose one bounded code, rule, prompt, model, or configuration change.
4. Run deterministic unit/integration tests and the relevant evaluation suite.
5. Compare aggregate metrics and important per-slice regressions with the baseline.
6. Stop after the configured attempt or budget limit.
7. Present failures, costs, and changed behavior for review.
8. Promote only when required gates pass and any configured approval is granted.

Suggested measures include field precision/recall, exact match, normalized edit
distance, table cell accuracy, part-number verification precision, retrieval recall at
K, mean reciprocal rank, citation correctness, answer faithfulness, no-answer
accuracy, latency, and cost. Metric targets must be selected from the actual use case;
one universal score is not sufficient.

User corrections enter a review queue. They do not immediately become prompts,
schemas, or golden truth. Accepted corrections create versioned evaluation cases and
may trigger a later candidate cycle.

## HAL front-end contract

Once the RAG core is stable, a HAL extension can expose narrow tools. Suggested
read-only tools are:

- `rag_status`: source, index, parser, and evaluation health.
- `rag_sources`: approved source summaries and latest scan status.
- `rag_search`: bounded ranked results with scores and provenance.
- `rag_show`: one source item or extraction by stable ID.
- `rag_ask`: grounded answer plus citations and query trace ID.
- `rag_review_list`: uncertain extractions and verification conflicts.
- `rag_eval_report`: baseline/candidate metrics and regression slices.

Separate mutating tools are:

- `rag_scan` and `rag_ingest`.
- `rag_review_resolve`.
- `rag_feedback_add`.
- `rag_reindex`.
- `rag_candidate_promote`.

The model must not provide database paths, model IDs, broader capabilities, or source
roots as tool arguments. Those values come from reviewed configuration. Mutating and
cost-incurring operations should be distinguishable in capability and approval policy.

Skills may provide conversational routines such as `/rag-ask`, `/rag-review`, and
`/rag-evaluate`, but skills do not implement parsing, indexing, migrations, or durable
review state.

## Workflow design

### Process usable with HAL today

Run the project from a dedicated workspace containing the RAG code and only approved
source mounts or adapters.

1. Invoke `/design` with the locations referenced by stable IDs from the private source
   manifest. Ask for inventory, unknowns, risks, and alternatives only.
2. Review the design response and deterministic discovery artifacts. Modify the source
   manifest or constraints until acceptable.
3. Invoke `/plan` to define milestone boundaries, schemas, tests, evaluation fixtures,
   and acceptance thresholds. Review and approve the plan.
4. Invoke `/workflow feature <milestone>` for one testable vertical slice, such as
   inventory, one parser, provenance storage, lexical retrieval, or one HAL tool.
5. Inspect the harness outcome and evaluation report before starting the next slice.
6. Run `/review` or another feature workflow for bounded corrections. Commit and push
   only through a separate explicit request.

Do not place the entire build in one `/workflow feature` request. The current workflow
does not pause between its phases and is not a durable project orchestrator.

### Proposed `rag-build` workflow

A future checkpointed workflow should use explicit typed artifacts and approval gates:

```text
configure sources
      |
      v
discover --[operator approval]--> architecture --[operator approval]--+
                                                                  |
                                                                  v
foundation -> ingestion pilot -> multimodal pilot -> retrieval pilot
      |              |                  |                 |
      +---- verify --+------ verify ----+------ verify ---+
                                                                  |
                                                                  v
entity verification -> HAL integration -> acceptance evaluation
          |                    |                    |
          +-------- review / bounded repair -------+
                                                   |
                                      [operator promotion approval]
```

Required workflow capabilities beyond the current implementation are:

- Durable checkpoints and safe resumption.
- User-defined or first-class `rag-build` phase definitions.
- Typed phase outputs rather than prose-only handoffs.
- Approval gates that can pause without treating the workflow as failed.
- Conditional branches for unsupported formats and failed quality thresholds.
- Milestone-level budgets and retry limits.
- Structured headless receipts and an aggregate project status view.
- Dataset/evaluation version pinning in every build or promotion phase.

The durable RAG database and build artifacts remain authoritative even after these
workflow features exist.

## Harness profile

A starting project configuration can apply conservative limits and deterministic
checks:

```yaml
only_write_locally: true
bash_policy: approve

harness:
  default_capability: change
  budgets:
    provider_calls: 40
    tool_calls: 160
    elapsed_seconds: 1200
    input_tokens: null
    output_tokens: null
  verification:
    - name: unit-and-integration
      command: python -m pytest -q
      timeout_seconds: 300
      required: true
    - name: rag-evaluation
      command: python -m rag_system.eval --suite tests/fixtures/golden --json
      timeout_seconds: 600
      required: true
  repair_attempts: 1
```

Exact budgets must be established from observed runs. Verification uses synthetic or
explicitly approved fixtures; it must not silently evaluate the entire private corpus.
Expensive ingestion, embedding, reindexing, or model-evaluation commands should have
separate approvals or constrained extension tools rather than relying on unrestricted
shell prompts.

## Delivery milestones

1. **Discovery tooling:** private source manifest, deterministic inventory, sampling,
   schema profiles, and a reviewed design report.
2. **Core storage:** migrations, source identity, hashing, versions, provenance,
   ingestion runs, and a standalone CLI.
3. **Text ingestion pilot:** one high-value format, parser fixtures, normalization,
   lexical chunks, and idempotent re-ingestion.
4. **Retrieval baseline:** FTS search, filters, citations, context packets, query traces,
   and no-answer behavior.
5. **Multimodal pilot:** image inventory, OCR, one approved vision fallback, evidence
   coordinates, and reviewed quality metrics.
6. **Domain verification:** part-number/entity rules, authoritative lookup adapter,
   ambiguity states, and review queue.
7. **Optional hybrid retrieval:** embeddings, fusion/reranking, offline comparison, and
   lexical fallback.
8. **HAL integration:** read-only extension tools and skills, then separately approved
   ingestion, feedback, review, and promotion tools.
9. **Operational hardening:** backups, restore, reindexing, deletion/retention, access
   profiles, observability, and recovery tests.
10. **Checkpointed workflow:** implement only after its state, approval, cancellation,
    and resumption semantics are accepted for HAL generally.

## Security and trust requirements

- Treat every source document, image, OCR result, and retrieved excerpt as untrusted
  input that may contain prompt injection.
- Apply access filters before retrieval context is constructed, not after generation.
- Keep credentials, source roots, and sensitive endpoint settings out of prompts,
  sessions, journals, and tracked configuration.
- Use least-privilege read credentials for discovery and separate credentials for any
  mutating connector.
- Record which parser or model received each source item and why it was eligible.
- Redact secrets and unnecessary personal data from logs, traces, and evaluation
  exports.
- Require explicit authorization for uploads, dependency installation, external
  service creation, deletion, retention changes, and production promotion.
- Test backup, restore, rebuild, and source-deletion behavior before relying on the
  system.
- Use an OS sandbox, container, VM, or equivalent boundary appropriate to the data;
  the HAL harness is not that boundary.

## Acceptance criteria

The first complete RAG system is acceptable when:

1. Discovery inventories all configured sources and clearly distinguishes full scans
   from bounded content samples.
2. Repeated ingestion is idempotent and changed or deleted inputs follow documented
   version and retention behavior.
3. Every normalized field, chunk, entity, and answer citation resolves to a specific
   source version and location.
4. Unsupported, corrupt, protected, and excluded inputs produce visible typed outcomes.
5. Text retrieval works without embeddings; optional hybrid retrieval demonstrates a
   measured improvement on an approved evaluation set.
6. Image extraction records method, version, confidence, and source coordinates, and
   uncertain results enter a review queue.
7. Part numbers or equivalent identifiers are never labeled verified solely from model
   confidence.
8. Grounded answers cite evidence and return an explicit insufficient-evidence result
   when appropriate.
9. Candidate extraction or retrieval changes cannot replace the accepted baseline
   unless required tests and quality gates pass.
10. HAL can inspect status, search, ask, show evidence, and review uncertainty through
    constrained tools while the RAG core remains independently operable.
11. Harness journals and RAG audit records contain no prompts, credentials, or raw
    private corpus content unless a separate reviewed policy explicitly requires it.
12. Backup, restore, reindex, cancellation, and interrupted-ingestion recovery are
    tested with synthetic fixtures.

## Open decisions for the discovery phase

- Which data locations, formats, approximate volumes, and change rates are in scope?
- Which sources contain images, scans, tables, or attachments?
- What data may be sent to which OCR, embedding, reranking, or vision endpoints?
- What authoritative catalogs or validation rules exist for part numbers and other
  domain entities?
- What latency, freshness, offline, deployment, and concurrency requirements apply?
- Which answer types need exact facts, document discovery, synthesis, or structured
  exports?
- What reviewed examples can form the initial evaluation suites?
- Which errors are tolerable, which require human review, and which must fail closed?
- What retention, deletion, backup, and access-control policies apply?
- Should the initial HAL adapter use an in-process Python API or a versioned JSON CLI?

