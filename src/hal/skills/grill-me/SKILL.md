---
name: grill-me
description: Stress-test a plan or design through a rigorous, one-question-at-a-time interview until its decisions, dependencies, risks, and unresolved branches are understood. Use when the user asks to be grilled or wants a plan challenged before acting on it.
---

# Grill Me

Interview the user rigorously about the plan or design until both sides share a
clear understanding of it. Treat any arguments as the initial subject of the
interview.

Inspect relevant repository evidence read-only before asking anything that the
codebase can answer. Do not modify files or begin implementation during the
interview unless the user separately asks for that work.

Build and follow the decision tree dynamically rather than reciting a fixed
questionnaire. Identify consequential choices, assumptions, dependencies, failure
modes, tradeoffs, edge cases, and acceptance criteria. Follow each relevant branch
until it is resolved, explicitly deferred, or identified as blocked. Revisit earlier
answers when a later decision exposes a conflict, but do not repeat settled questions
without a concrete reason.

Ask exactly one focused question at a time. With each question, provide a recommended
answer and a concise rationale or tradeoff so the user can accept it, revise it, or
choose another direction. Distinguish repository facts and the user's decisions from
your recommendations and assumptions. Challenge vague or inconsistent answers
directly and constructively.

When all consequential branches are sufficiently resolved, summarize the shared
understanding:

- the plan's objective, scope, and non-goals;
- accepted decisions and the reasons behind them;
- dependencies, risks, failure handling, and acceptance criteria;
- deferred decisions, assumptions, and any remaining blockers.

Ask the user to confirm or correct that summary. Stop after the interview and recap;
do not silently transition into implementation.
