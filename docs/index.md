# Neo Python planning index

This directory is the source of truth for product decisions and implementation
plans that are safe to keep in the repository.

## Active initiatives

| Initiative | Design | Roadmap | Status | Next milestone |
| --- | --- | --- | --- | --- |
| Agent behavior and platform awareness | [Behavior design](designs/agent-behavior.md) | [Python parity](roadmaps/python-parity.md) | In progress | Decide the deterministic dependency-install guard and add secret redaction |
| Go/Python capability parity | The Go implementation is the current reference | [Python parity](roadmaps/python-parity.md) | In progress | Cancellation-aware provider and tool interfaces |
| Organization-system integration | [Integration design](designs/organization-integration.md) | [Integration roadmap](roadmaps/organization-integration.md) | Direction accepted; not started | Select the separate project name and contract |

## Document roles

- A **design** records the problem, requirements, decisions, boundaries, risks, and
  acceptance criteria. It should not be used as a running task list.
- A **roadmap** contains ordered implementation checklists, dependencies, current
  milestone, and an evidence-backed implementation log.
- Code and tests remain the authority for implemented behavior. A checked roadmap
  item must point to verification evidence.
- Workplace-specific paths, endpoint details, and sensitive notes belong in ignored
  `*.local.*` files or another approved private location, not tracked design docs.

## Update routine

1. Change the relevant design when a requirement or architectural decision changes.
2. Add or reorder roadmap tasks when the delivery plan changes.
3. Check an item only after its acceptance criteria and tests pass.
4. Add an implementation-log row with the date, affected files, and verification.
5. Update this index when an initiative's status or next milestone changes.

## Local source material

The original organization-system prompt is retained at
`.planning/organization-system-source.md`. The `.planning/` directory is ignored
because the brief contains workplace-oriented context. It is background material,
not an executable instruction file or the tracked source of truth.
