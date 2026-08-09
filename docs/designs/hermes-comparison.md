Yes—I would change the recommendation significantly.

The current document treats HAL mostly as Jellyfin-related code that needs Hermes as its agent layer. After inspecting the repository, that premise is no longer accurate. HAL already has:

- A provider-neutral agent loop
- Shell, filesystem, and structured Git tools
- Sessions and resume support
- Project instructions, skills, and phases
- Interactive and headless operation
- An opt-in Python extension system

In particular, HAL already has the clean integration point Jellyfin needs: separately installed extensions that are explicitly enabled in configuration ([README.md](/home/arrakis/projects/hal/README.md:250), [extensions.py](/home/arrakis/projects/hal/src/hal/extensions.py:41)).

## My revised recommendation

Use **HAL as your primary agent**, especially at work.

Keep Jellyfin as a separate `hal-jellyfin` extension package:

```text
HAL
 ├── core coding-agent functionality
 └── optional hal-jellyfin package
       ├── Jellyfin API
       ├── Billboard/SQLite
       ├── matching
       └── playlist tools
```

This separation is good architecture. Jellyfin does not need to be built into HAL, and “not integrated into the core repository” does not mean it is disconnected. HAL’s entry-point extension interface is the integration boundary.

I would therefore remove or reverse the recommendation that:

> Hermes should become the brain/interface to the HAL ecosystem.

Instead:

> HAL should remain the lightweight, inspectable agent. Hermes can be an optional reference implementation or personal experimentation environment when its additional capabilities justify its considerably larger footprint.

## Why the workplace constraint matters

HAL’s required runtime dependency is currently only PyYAML. Rich, Textual, and Dulwich are optional ([pyproject.toml](/home/arrakis/projects/hal/pyproject.toml:12)). That is a meaningful advantage in a controlled workplace environment:

- Fewer packages to approve and audit
- Smaller software-supply-chain surface
- No automatic browser, Node, messaging, memory, scheduling, or integration stack
- Easier source review
- Easier installation through an internal Python package process
- Less unexplained background behavior

Hermes’s current installer can install or manage `uv`, Python, Node.js, browser components, Git tooling, and a broad set of Python extras. Hermes has improved its supply-chain posture through exact pins and a hash-bearing lockfile, but it remains a substantially larger system. Its own installer also has fallback paths that resolve packages from PyPI when locked installation fails. Those are reasonable product decisions, but they are poorly matched to your workplace restriction. See the official [Hermes dependency manifest](https://github.com/NousResearch/hermes-agent/blob/main/pyproject.toml) and [installer](https://github.com/NousResearch/hermes-agent/blob/main/scripts/install.sh).

Thus your concern is not merely “HAL is smaller, so it feels safer.” It is a defensible operational distinction:

> HAL has a smaller auditable and installable footprint, while Hermes has a richer feature set with a correspondingly larger dependency and capability surface.

## Important security qualification

I would not describe HAL itself as secure or sandboxed. HAL gives the model a shell and file-writing tools. Its approval-prefix mechanism is explicitly UI friction rather than a security boundary.

Also, enabled extensions execute ordinary Python in the HAL process. Loading an extension imports its entry point and calls its factory ([extensions.py](/home/arrakis/projects/hal/src/hal/extensions.py:60)). Consequently, a malicious or compromised extension can access everything HAL can access.

The defensible workplace security story is:

1. HAL and every extension are installed from reviewed, controlled sources.
2. Only explicitly approved extensions are installed and enabled.
3. HAL runs with ordinary least-privilege OS credentials.
4. The model uses an approved enterprise endpoint.
5. Sensitive repositories or credentials remain outside its execution environment where practical.
6. For stronger isolation, HAL runs in a container, VM, or restricted workstation environment.

Lightweight improves reviewability and supply-chain exposure; it does not create process isolation.

## MCP recommendation

I would no longer recommend making Jellyfin an MCP server immediately.

Start with a HAL extension because:

- You already have the extension mechanism.
- Jellyfin is currently for HAL.
- It avoids another runtime, protocol, package set, process, and configuration layer.
- It is easier to deploy and audit.

Introduce an MCP wrapper later only if you genuinely need the same Jellyfin tools from HAL, Hermes, Codex, Claude, and other clients. MCP should be an adapter around the shared Jellyfin library—not the primary architecture by default.

## What Hermes should influence

Hermes is still valuable as a feature catalog. You can selectively add capabilities to HAL when you actually need them:

- Better context compaction
- More restrictive tool policy
- Optional memory
- Scheduling as a separate service
- Browser support as an optional extension
- MCP client support, if real interoperability demand appears
- More granular extension permissions

That produces a better direction than adopting Hermes wholesale:

```text
Keep HAL small
      │
      ├── add capabilities only when justified
      ├── keep integrations optional
      └── borrow proven ideas without inheriting the entire stack
```

So the short answer is: **keep HAL, keep Jellyfin external but integrated through HAL’s extension interface, and do not put Hermes above HAL.** For your actual work environment, HAL’s small dependency footprint and inspectability are core product requirements, not missing features.