**Codex uses both.** The architecture is closer to a hybrid of an agentic LLM **plus** workflows/skills/tools than simply “give an agentic model a goal and hope it figures everything out.”

OpenAI actually describes the Codex **agent loop/harness** as the core logic that orchestrates the user, model, and tools. The model reasons about what to do, invokes tools, observes the results, and continues working toward the goal. ([OpenAI][1])

A useful mental model is:

```text
                 GOAL
                  │
                  ▼
        ┌───────────────────┐
        │   Agentic LLM     │
        │ reason / plan     │
        │ decide next step  │
        └─────────┬─────────┘
                  │
            Codex Harness
          / Agent Loop
                  │
        ┌─────────┼──────────┐
        ▼         ▼          ▼
      Tools     Skills    Environment
        │         │          │
     shell     workflows    files
     git       procedures   repo
     tests     scripts      terminal
     APIs      standards
        │         │
        └────┬────┘
             ▼
           RESULT
             │
             ▼
      inspect / verify
             │
         not done?
             │
             └──────────────► Agent
```

### The interesting part for HAL

This is actually **very close to the architecture I think makes sense for HAL**.

You shouldn't make HAL either:

```text
LLM does everything
```

or

```text
hard-coded workflows do everything
```

Instead:

```text
HAL
│
├── Agent
│     figures out WHAT needs to happen
│
├── Tools
│     deterministic operations
│
├── Skills
│     reusable ways of accomplishing common goals
│
└── Workflows
      deterministic/semi-deterministic sequences
      for things where consistency matters
```

And Codex now explicitly has this concept. OpenAI describes **Skills as the authoring format for reusable workflows**. A skill can contain instructions, reference material, assets, and executable scripts. Codex can select the skill automatically based on what the user asks, or you can explicitly tell it to use one. ([OpenAI Developers][2])

That's an important distinction.

For example, imagine your HAL music agent gets:

> "Make me a really good 80s rock playlist."

You *could* let the LLM completely improvise:

```text
LLM
 ├─ inspect library
 ├─ figure out years
 ├─ figure out genres
 ├─ figure out popularity
 ├─ make playlist
 └─ hope everything is correct
```

But a Codex-like architecture would be closer to:

```text
User goal
   │
   ▼
HAL Agent
   │
   ├── recognizes: playlist-building skill
   │
   ▼
80s Playlist Skill
   │
   ├─ 1. query Jellyfin library
   ├─ 2. filter 1980-1989
   ├─ 3. filter rock genres
   ├─ 4. lookup Billboard/ranking data
   ├─ 5. score candidates
   ├─ 6. ensure artist diversity
   ├─ 7. create playlist
   └─ 8. verify songs actually exist
```

Your existing Python functions remain underneath:

```text
get_library()
filter_by_year()
filter_by_genre()
get_billboard_rank()
create_playlist()
verify_playlist()
```

**The LLM shouldn't calculate or fake those things.** It decides *when and why* to use those deterministic functions.

That's exactly where agentic systems become much more reliable.

### Codex has several layers beyond the model

As of August 2026, I'd break Codex into roughly five layers:

| Layer                    | Purpose                                                  |
| ------------------------ | -------------------------------------------------------- |
| **Agentic coding model** | Reasoning, planning, deciding what to do                 |
| **Codex agent harness**  | Runs the observe → reason → act → observe loop           |
| **Tools**                | Terminal, files, git, tests, APIs, etc.                  |
| **Skills**               | Reusable workflows/instructions/scripts                  |
| **Orchestration**        | Multiple agents, scheduling, worktrees, longer workflows |

OpenAI specifically says Codex Skills can bundle instructions, resources, and scripts so Codex can **“run workflows”** reliably. ([OpenAI][3])

And Codex goes beyond skills. The current product supports multiple agents working in isolated worktrees, and scheduled/background work such as issue triage, monitoring, and CI/CD tasks. ([OpenAI][4])

At an even higher level, OpenAI has **Symphony**, an orchestration design where an external state machine manages Codex agents. For example:

```text
Linear issue
     │
     ▼
   TODO
     │
     ▼
Symphony starts Codex
     │
     ▼
 IN PROGRESS
     │
     ├── Codex plans
     ├── Codex codes
     ├── Codex tests
     └── Codex fixes failures
     │
     ▼
   REVIEW
     │
     ▼
    DONE
```

OpenAI describes Symphony as turning the issue tracker into the control plane, with each active task receiving an agent and the workflow status acting as a state machine. ([OpenAI][5])

That is **workflow orchestration around an agent**, rather than expecting the LLM itself to remember the entire business process.

---

### This is the line I would draw for HAL

There's a useful principle here:

**Agent = decides.
Skill = knows how.
Tool = does.
Workflow = controls the process.**

For example:

```text
"Play some music I would like."

AGENT
"What does Bart probably mean?"

        ↓

SKILL
"Use the music recommendation procedure."

        ↓

TOOLS
query_library()
query_billboard()
score_tracks()
create_playlist()

        ↓

WORKFLOW
Discover → Rank → Validate → Create → Verify

        ↓

AGENT
Inspect result and decide whether the goal was satisfied.
```

That is considerably stronger than simply exposing ten Jellyfin functions to an LLM.

And this also explains something you've noticed with CLI agents: **the impressive behavior doesn't come entirely from the model.** A big part comes from the *agent harness surrounding the model*—context management, tool execution, skill selection, verification loops, environment management, and orchestration. OpenAI explicitly makes that distinction when describing Codex's agent loop. ([OpenAI][1])

For HAL, I would actually take inspiration from Codex and evolve it toward **Agent → Skills → Tools → Workflows**, rather than simply adding more and more tools. Your existing deterministic Jellyfin functions are already a very good foundation for that architecture.

[1]: https://openai.com/index/unrolling-the-codex-agent-loop/ "Unrolling the Codex agent loop | OpenAI"
[2]: https://developers.openai.com/codex/build-skills "Build skills | ChatGPT Learn"
[3]: https://openai.com/index/introducing-the-codex-app/ "Introducing the Codex app | OpenAI"
[4]: https://openai.com/codex/ "Codex in ChatGPT | AI Coding Agents for Software Engineering | OpenAI"
[5]: https://openai.com/index/open-source-codex-orchestration-symphony/ "An open-source spec for Codex orchestration: Symphony. | OpenAI"
