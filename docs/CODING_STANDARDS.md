# MNM Coding Standards

This document defines the coding standards for MNM contributions. It exists so that "follows project standards" is a checkable criterion, not a hand-wave. Sonnet (via Claude Code) self-checks against this document before declaring any code-touching task complete.

This is a living document. When standards change, this doc changes first, then the codebase is brought into alignment.

## Audience and Scope

These standards apply to all source code in the MNM repo: Python (controller, scripts, plugin), JavaScript (controller frontend), shell scripts, Dockerfile and docker-compose YAML, SQL migrations, and configuration files.

They do not apply to vendored third-party code (e.g., the in-place patches to `nautobot-device-onboarding` — those follow upstream conventions for diff clarity).

## Core Philosophy

**Boring is good.** Standard library over third-party. Plain functions over classes unless state justifies it. Long names over clever names. The reader is the next person to debug this at 2 AM, possibly with limited Python experience — make it easy for them.

**Async by default for MNM's workloads.** Network devices are unpredictable. Small SOHO appliances stall. Devices with busy management planes time out under load. Sync code that talks to a network device blocks the whole worker on the slowest device. Async lets MNM keep working while a flaky switch decides whether to answer. Default to async; deviate only when the work is provably CPU-bound or trivially fast.

**Loud failures with deduplication.** Silent failures are how MNM ends up reporting "everything healthy" while sweeping nothing. Loud failures matter. But repeated identical failures from the same source must deduplicate — one ERROR every minute is signal; the same ERROR thousands of times in a polling cycle is noise that buries other problems.

**Documentation is part of the code, not a separate task.** A function without a docstring is incomplete. A module without a module-level docstring is incomplete. A new dependency without a justification comment is incomplete.

**Rule 8 applies to code, not just docs.** No real IPs, hostnames, or credentials in source files, comments, log examples, test fixtures, or commit messages. RFC 5737 ranges only.

---

## Python Standards

### Version and Style

- **Python 3.11+** (matches the Nautobot base image and the controller's runtime)
- **PEP 8** baseline, with these explicit choices:
  - Line length: 100 characters (not 79). Long descriptive names are common in network code; 79 is too constraining.
  - String quotes: double quotes (`"`) for human-readable strings, single quotes (`'`) for dict keys and identifiers
  - Trailing commas in multi-line collections and function signatures
- **Type hints required** on all function signatures and class attributes. Optional on local variables when type is obvious from context.
- **No dead code.** Unused imports, unused variables, commented-out code blocks are removed before commit. (Rule from CLAUDE.md: "no deprecated code in commits.")

### Async First

MNM is an async-first codebase because its work is dominated by IO against unpredictable network devices.

- **Default to async** for any function that talks to a network device, an API, a database, the filesystem, or another process
- **Sync is acceptable** for pure computation, in-memory data manipulation, and code that genuinely never blocks (parsing, formatting, validation)
- **HTTP calls** use `httpx.AsyncClient` (never `requests`)
- **Database operations** use SQLAlchemy async + asyncpg (never the sync engine)
- **SNMP** uses pysnmp's async HLAPI (`bulkCmd` as coroutine — see Phase 2.8 lessons learned for pysnmp 6.x specifics)
- **Subprocess calls** use `asyncio.subprocess` when the result matters; use `asyncio.to_thread()` for fire-and-forget sync code
- **Concurrency** uses `asyncio.gather()` with explicit concurrency limits (semaphores). Unbounded `gather()` over a large device list is a bug — pick a sensible limit (e.g., `MNM_COLLECTION_CONCURRENCY`)
- **Don't mix freely.** A module is either async-first or sync-first. Crossing the boundary requires `asyncio.to_thread()` (sync-from-async) or a clearly-documented entry point pattern. Modules that mix without explanation are a code smell.
- **Never use `asyncio.run()` inside library code** — only at entry points (FastAPI handles this for the controller)

### Imports

- Standard library first, third-party second, local third (PEP 8 grouping)
- One import per line for readability
- Absolute imports only (`from controller.app.db import init_db`, not `from ..db import init_db`)
- No wildcard imports (`from foo import *`) — they break grep and surprise readers

### Exception Handling

This is the area most likely to drift, so it gets the most detail.

**Default rule: let exceptions propagate.** Catch only when you have a specific recovery action.

**Specific exception types only.** `except Exception:` is acceptable in three places and three places only:
1. Top-level entry points (the request handler, the polling loop) where uncaught exceptions would crash the process
2. Background task wrappers where you log the exception and continue with the next iteration
3. When integrating with code that genuinely raises arbitrary exception types (some vendor SDKs)

In all three cases, the broad catch must:
- Log the exception with full traceback at ERROR level
- Include enough context in the log message to identify what was being done (device name, IP, operation type)
- Re-raise if the caller needs to know, or document why swallowing is correct

**Bare `except:` (no exception type) is forbidden.** It catches `KeyboardInterrupt` and `SystemExit` and breaks shutdown.

**Never swallow exceptions silently.** Even if the recovery is "log and continue," the log line must explain what happened and why continuing is acceptable.

**Re-raising preserves traceback.** Use `raise` (no argument) to re-raise the current exception. Use `raise NewException(...) from original` when wrapping in a new exception type — this preserves the chain.

The 119 broad exception catches called out in Phase 2.9 are the working list of sites to tighten during v1.0. New code does not contribute to that count.

### Logging

- **Use `structlog` consistently** (already in use in the controller per `StructuredLogger`)
- **Log levels:**
  - DEBUG: developer-only detail, expected to be turned off in production
  - INFO: normal operational events someone running MNM cares about (sweep started, polling completed, device onboarded)
  - WARNING: something unexpected happened but the system handled it (retry triggered, fallback activated, slow request)
  - ERROR: something failed that an operator needs to investigate (DB unreachable, NAPALM auth failed, sweep aborted)
  - CRITICAL: the system cannot continue (use rarely — most "fatal" errors are actually ERROR + process exit)
- **Structured fields, not f-strings in the message.** Good: `log.info("sweep_started", cidr=cidr, scope_id=scope_id)`. Bad: `log.info(f"Sweep started for {cidr} (scope {scope_id})")`. Structured fields are queryable; f-strings are not.
- **Event names are snake_case nouns or noun_verb pairs.** Examples: `db_init`, `sweep_started`, `polling_failed`, `napalm_session_exhausted`. They are stable identifiers — treat them like API names.
- **Do not log secrets.** Passwords, tokens, full credential dicts. If you must log a credential context, log the username/key-id and an `auth_method` field, never the secret itself.
- **Do not log to stdout/stderr directly.** Use the logger.

#### Deduplication and Rate Limiting

Repeated identical errors must be deduplicated to preserve the signal-to-noise ratio of logs. The EX3300 NETCONF exhaustion problem is the canonical example: a single broken device could otherwise spam thousands of identical errors in a polling cycle.

**Patterns:**

- **First/Nth/last logging.** When a known-likely-to-repeat error happens, log the first occurrence at full verbosity, then at exponentially-spaced intervals (1st, 2nd, 4th, 8th, 16th...), then a summary line on recovery: `napalm_session_exhausted_recovered, suppressed=247, duration_sec=312`.
- **Per-source dedup keys.** Errors are deduplicated by source identity (device IP, job type, error class) — not just by message text. Two devices both timing out are two distinct problems; the same device timing out 50 times in a row is one problem.
- **Time-windowed dedup.** A 5-minute suppression window is a reasonable default for transient errors. After the window, the next occurrence logs at full verbosity again (so we re-notice if the problem persists).
- **Always log on state change.** Going from "failing" to "succeeded" must always log at INFO — operators need to know when a problem self-resolves.

The controller's `StructuredLogger` should grow a `dedup` helper or wrapper for this pattern. New code that's likely to log repeatedly should use it. Code that logs at most once per operation does not need dedup.

When in doubt: **would 1,000 of this log line in 60 seconds be useful or terrible?** If terrible, dedup.

### Type Hints

- All function parameters and return types annotated
- Use `from typing import` for compatibility-wrapper types when needed; prefer built-in generics (`list[int]`, not `List[int]`) on 3.11+
- `Optional[X]` is acceptable; `X | None` is preferred (3.10+ syntax)
- `Any` is a smell. Use it only when interfacing with truly untyped external data (e.g., a JSON response with variable schema), and add a comment explaining why
- Pydantic models for API request/response bodies, not raw dicts

### Docstrings

- **Required on every public function, class, and module.**
- Style: Google or NumPy — pick one per module and stay consistent. Existing code uses Google-ish style; new code should match unless the module is already NumPy-style.
- Module docstring explains the module's role in the architecture (one paragraph minimum)
- Function docstring explains *what* it does, not *how*. The how is the code itself.
- Document raised exceptions in the docstring when they're part of the contract

### Dependencies

- **Adding a new top-level dependency requires justification.** Justify in the commit message: what does it do, why isn't stdlib enough, what's the maintenance status (last release, GitHub stars/issues, license).
- Pin to a specific version in `requirements.txt` or `pyproject.toml` (not `>=`)
- Prefer well-maintained, single-purpose libraries over swiss-army-knife frameworks
- If a dependency is needed in only one module, consider whether you can write the 30 lines yourself instead

### Testing

(v1.0 introduces a real test suite per CLAUDE.md section 1.10. Until then, this section is forward-looking.)

- pytest + pytest-asyncio
- Test files mirror source layout: `tests/unit/test_db.py` tests `controller/app/db.py`
- Test names start with `test_` and describe the scenario: `test_init_db_retries_on_initial_connection_refusal`
- One assertion per test where reasonable; multiple assertions OK if testing related properties of one operation
- Mock at the boundary, not internally — mock `httpx.AsyncClient.get`, not `nautobot_client._cached_devices`
- No real network calls in unit tests. Integration tests that touch real services are clearly marked and runnable separately

### File Layout

- One module per concern. `db.py` for database, `nautobot_client.py` for Nautobot API, etc.
- Modules over 500 lines are a code smell — consider whether they should be split.
- Avoid `utils.py` and `helpers.py` — they become dumping grounds. Name modules by what they do.

---

## JavaScript / Frontend Standards

The controller frontend is intentionally minimal: vanilla JS, no build step, no framework. This is a constraint, not a deficiency. Keep it that way.

### Style

- ES2020+ syntax (the controller targets modern browsers — no IE compatibility needed)
- 2-space indentation (vanilla JS convention)
- `const` by default, `let` when reassignment is needed, never `var`
- Semicolons at end of statements (consistency with existing code)
- Double quotes for strings to match Python convention

### Patterns

- **No external runtime dependencies via CDN.** Everything ships in the container.
- **No bundler, no transpiler, no framework.** If you reach for React/Vue/Svelte, stop and reconsider.
- **DOM manipulation directly.** `document.getElementById`, `element.classList.add`, etc. No jQuery.
- **Fetch API for HTTP.** `await fetch('/api/...')`. No axios.
- **Module pattern via `<script>` includes.** Each JS file exposes a global namespace object (`MNMServiceURLs`, etc.) — this is the existing pattern, keep it consistent.

### Async

- `async`/`await` for fetch calls, never raw `.then()` chains
- All `fetch` calls wrapped in try/catch — the catch logs and shows a user-visible error indicator

### Hardcoded Values

- **No hardcoded URLs, ports, or IPs in JS files.** Use the `MNMServiceURLs` module (per the Phase 2.9 service URL centralization decision). Adding a new hardcoded URL is a regression.

---

## Docker / Compose Standards

- **Pin image versions.** `nautobot:3.0-py3.11`, not `nautobot:latest`.
- **Document the pin.** Every pinned image gets a comment in `docker-compose.yml` explaining:
  - Why this version specifically (e.g., "3.0 required by nautobot-device-onboarding 5.x")
  - When we should consider upgrading (e.g., "Upgrade when 4.x ships and onboarding plugin supports it")
  - Any known incompatibilities with newer versions (e.g., Traefik v3 Docker API issue)
  - The pin rationale also goes into `docs/CHANGELOG.md` when changed, so version bumps are traceable
- **Healthchecks on every service** that has a queryable health endpoint. Services without one (gnmic, traefik) are exceptions — document why with an inline comment.
- **Comments on non-obvious choices.** If a service has unusual env vars or a custom command, comment why.
- **No secrets in compose files.** Everything sensitive comes from `.env`. The compose file references `${VAR}`.
- **Single compose file** per the existing decision — no compose-override files.

---

## Shell Script Standards

- `#!/usr/bin/env bash` shebang
- `set -euo pipefail` at the top of every script
- Comment header explaining purpose, prerequisites, and usage
- Functions for anything called more than once
- Use `[[ ]]` for tests, not `[ ]`
- Quote all variable expansions (`"$var"`, not `$var`) unless you specifically want word splitting

---

## Git Standards

(Most of this is covered in CLAUDE.md "Git Workflow" and "Commit Discipline" sections — repeated here for completeness.)

### Current Workflow (through v0.9.x)

The project uses a simplified flow appropriate for single-contributor hotfix work:
- **`main`** — primary branch, holds tagged releases and post-tag documentation/hotfix commits
- **`fix/<short-name>`** — short-lived branches for individual bug fixes, PR'd or merged directly to main, then deleted
- **Tags** — semantic versioning (`v0.9.0`, `v0.9.1`, etc.) marking releases

Documentation-only commits (CHANGELOG updates, doc additions) may go directly to main.

### Future Workflow (when v1.0 work begins)

When v1.0 architectural work starts, multiple workstreams will be in flight simultaneously (mnm-plugin, SNMP collector, FortiOS connector, etc.). At that point we adopt:
- **`main`** — protected, tagged releases only reach here via merge from develop
- **`develop`** — integration branch for unreleased work; feature branches merge here
- **`feature/<short-name>`** — short-lived branches for individual features, PR'd to develop
- **`fix/<short-name>`** — bug-fix branches; may target develop (for unreleased fixes) or main (for hotfixes against a shipped tag)

The transition to the develop-based flow happens with the first v1.0 feature prompt, not before. CLAUDE.md will be updated to mark the transition.

### Commits

- **Conventional Commits format**: `type: short description`
  - Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `perf`
- **Subject line under 72 characters.** Imperative mood ("add retry loop", not "added retry loop").
- **Body explains why, not what.** The diff shows what changed; the body explains why it changed and what alternatives were considered.
- **One logical change per commit.** Mixing a feature change with a refactor with a typo fix produces uncherrypickable history.

### Pre-commit Self-Check

Before declaring any code-touching task complete, Sonnet runs through this checklist against its own diff:

1. **Style:** Does the code match the conventions in this file? (line length, type hints, docstrings present, etc.)
2. **Async:** Is new IO-bound code async? Are concurrency limits in place where applicable?
3. **Exception handling:** Are catches specific? Are broad catches one of the three permitted cases? Are exceptions logged with context?
4. **Logging:** Are new log statements using structlog with structured fields? Are event names stable and snake_case? Is dedup applied where the log site is likely to repeat?
5. **Dependencies:** Were any new top-level dependencies added? If yes, is the justification in the commit message?
6. **Tests:** Were new functions added without test coverage? If yes, is there a clear reason (e.g., "test infra introduced in v1.0")?
7. **Docs:** Were any public API surfaces (functions, endpoints, env vars, config keys) changed without a corresponding docs update? Were any pinned image versions changed without a comment update?
8. **Rule 8:** Are there any real IPs, hostnames, or credentials in the diff?
9. **Rule 12:** If new data is collected/produced, does it surface in Nautobot somewhere? If not, is that explicitly tracked as a future plugin work item?
10. **No deprecated code:** Were old code paths replaced cleanly, with no lingering compatibility shims or feature flags?

If any check fails, fix before reporting done. If a check is intentionally skipped, document why in the commit message body.

---

## Standards Are Reviewed at Each Major Release

This document is reviewed at each major version bump (v1.0, v2.0, v3.0). Standards drift is normal as the codebase grows; periodic review catches it before it becomes intractable.

When this document changes:
1. The change is committed before any code change that depends on it
2. The codebase is brought into alignment with the new standard within the same release cycle
3. CHANGELOG.md notes the standards change so contributors know to read this file again