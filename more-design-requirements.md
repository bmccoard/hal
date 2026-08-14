That may actually be the right conclusion.

For **your use case**, HAL has one major advantage that most “Claude Code alternatives” don’t: it is already **built around the things you specifically want it to do**. You already have deterministic Python tools, Jellyfin integration, your own CLI, and an architecture you understand and can modify.

Most of the projects we’ve looked at mainly add a nicer harness around the same basic loop:

1. Give the model a goal.
2. Let it inspect available tools.
3. Model chooses a tool.
4. Execute the tool.
5. Feed the result back.
6. Repeat until done.

That core loop is not especially complicated. The value is in the **tooling and orchestration around it**.

For HAL, I’d probably evolve what you already have rather than replace it. The biggest upgrades would be:

* **Agent loop** — HAL keeps working until the requested goal is complete rather than doing one tool call at a time.
* **Planning** — model creates a short task plan and updates it while working.
* **Tool registry** — Python functions automatically expose their name, description, parameters, and schema.
* **Skills** — markdown/Python packages that teach HAL how to perform larger jobs using multiple tools.
* **Workflows** — deterministic sequences for things that should not depend entirely on model judgment.
* **Subagents** — let HAL delegate bounded tasks to another model/context.
* **Memory/context** — persistent information about your library, preferences, projects, etc.
* **MCP support** — lets HAL consume external MCP servers without you having to rewrite integrations.
* **Permissions** — read-only, write, shell, delete, network, etc., with approval rules.
* **Session persistence** — resume an interrupted task instead of starting over.
* **Context compaction** — summarize old agent history when the context gets large.

And importantly, you don't have to choose between **agentic behavior and workflows**.

A mature HAL could look like:

```text
                         HAL
                          │
                    Goal / Request
                          │
                    Agent Controller
                          │
             ┌────────────┼────────────┐
             │            │            │
          Skills       Workflows     Planner
             │            │            │
             └────────────┼────────────┘
                          │
                     Tool Registry
                          │
       ┌──────────────────┼───────────────────┐
       │                  │                   │
    Jellyfin           Files              Search
       │
    Playlists
       │
    Billboard DB

       + Email
       + Calendar
       + RAG
       + shell
       + Python
       + MCP servers
       + your future tools
```

For example, you could tell HAL:

> Make me a 100-song 1980s playlist with mostly popular songs, no more than 3 songs per artist, at least one song from every major artist I own, and put it in Jellyfin.

HAL could then autonomously:

```text
Plan
 ↓
query_library()
 ↓
query_billboard_rankings()
 ↓
compare_library_to_rankings()
 ↓
select_candidates()
 ↓
check_artist_distribution()
 ↓
revise_selection()
 ↓
create_jellyfin_playlist()
 ↓
verify_playlist()
 ↓
Done
```

Your existing **deterministic functions remain the important part**. The LLM decides *which ones to call and in what sequence*.

That's actually a stronger architecture than dumping your entire Jellyfin database into the model context and hoping it reasons correctly.

The projects like Claude Code, Nano Claude Code, Hermes, Open Interpreter, etc. are useful to study because they contain implementation ideas. But you don't necessarily need to adopt one wholesale.

I think the most useful exercise now would be to take **HAL's current architecture** and compare it feature-by-feature against Claude Code/Nano Claude Code/Hermes, then identify the ~5 features worth stealing rather than replacing HAL.
