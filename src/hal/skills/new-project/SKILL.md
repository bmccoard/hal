---
name: new-project
description: Interview the user to turn an incomplete software-project idea into an approved, implementation-ready brief before creating files or code.
---

# New Project Interview

Help the user define the project before creating it. Treat any arguments as the
initial idea, not as a complete specification.

Inspect existing repository evidence read-only when it can answer questions. Do not
create or edit files, initialize Git, install dependencies, scaffold code, or run a
mutating workflow during the interview.

Ask focused questions in small, manageable groups. Prioritize decisions that affect
scope or architecture, and follow up when an answer is ambiguous. Adapt to what is
already known instead of reciting a fixed questionnaire. Cover the relevant parts of:

- intended users, their problem, and primary workflows;
- goals, measurable success, scope, and explicit non-goals;
- target platforms, interfaces, data, and external integrations;
- security, privacy, safety, reliability, and regulatory constraints;
- technology constraints and meaningful user preferences;
- the smallest runnable milestone and observable acceptance criteria.

Distinguish evidence-backed facts from proposals, assumptions, and unresolved
decisions. Offer a small set of sensible options with tradeoffs when the user does
not know an answer, but do not select a consequential option for them. Avoid asking
about low-impact details that can safely follow repository conventions or be changed
later.

When the definition is sufficient, summarize:

- purpose and users;
- approved scope and non-goals;
- primary workflows and externally visible behavior;
- constraints and accepted technical or product decisions;
- smallest runnable milestone with acceptance criteria;
- assumptions, deferred decisions, and unresolved questions with blocking impact.

Then stop and ask the user to approve or correct the summary. Do not begin setup in
the same turn. After explicit approval, provide the accepted definition as a concise
project brief and wait for a separate request to scaffold or run a setup workflow.
