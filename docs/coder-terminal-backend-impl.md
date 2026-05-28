# Coder Terminal Backend Implementation Plan

> **For Hermes:** This document is an implementation plan, not the implementation itself. Keep changes scoped to the Coder terminal backend feature and validate each layer before moving on.

## Goal

Add a new terminal backend named `coder` to Hermes Agent so terminal-driven tools can execute commands inside a Coder workspace, with the same high-level behavior users already expect from existing backends such as `ssh`, `daytona`, and `modal`.

## Desired Outcome

After this work lands, a user should be able to configure:

- `terminal.backend: coder`
- the Coder-specific connection settings

…and then use the existing Hermes tool surface without changing prompts or workflows:

- `terminal`
- `process`
- `read_file` / `write_file` / `patch` / `search_files`
- `execute_code`

All of those tools should operate against the Coder workspace through the same environment abstraction used by the other terminal backends.

---

## Current Architecture Summary

## Dynamic workspace mapping decision

For dynamic Coder workspaces, Hermes should treat the workspace-binding key as a **logical conversation identity**, not as the current transcript fragment ID and not as the gateway routing key.

### Decision

Use the **lineage root `session_id`** as the key that maps a Hermes session to a Coder workspace.

Definition:

- if the current `session_id` has no `parent_session_id`, it is its own lineage root
- otherwise, follow `parent_session_id` repeatedly until reaching the top ancestor
- that root `session_id` is the workspace-mapping key

### Why this key

This choice matches Hermes's current session semantics better than the other available identifiers:

- **Do not use raw current `session_id`**
  - Hermes context compression creates a new child session with a fresh `session_id`
  - binding workspaces directly to the current `session_id` would incorrectly create a new workspace after each compression split
- **Do not use gateway `session_key`**
  - `session_key` is stable for a chat/thread/user slot
  - it typically survives `/new`, idle reset, and daily reset
  - binding workspaces to it would incorrectly reuse an old workspace after a true new-session boundary
- **Use lineage root `session_id`**
  - stays stable across compression continuations
  - changes when Hermes starts a genuinely fresh session with no parent linkage

### Implementation constraints

This design should preserve Hermes's existing session metadata model.

- resolve the lineage root **on demand**
- **do not** add new persistent session metadata fields such as `logical_session_id` or `root_session_id`
- prefer reusing Hermes's existing parent-chain traversal logic where possible
  - especially the lineage-resolution pattern already present in `tools/session_search_tool.py`

### Behavioral consequences

- normal continued conversation -> reuse the same Coder workspace
- context compression -> reuse the same Coder workspace
- `/new`, explicit reset, idle reset, daily reset -> create a new logical root, therefore a new Coder workspace
- session search / historical records remain unchanged; the workspace mapping logic is layered on top of existing session lineage

### Relevant code landmarks

- `run_agent.py`
  - agent-side `session_id` generation
  - `_compress_context()` creates a new child session and sets `parent_session_id`
- `gateway/session.py`
  - `build_session_key()`
  - `SessionStore.get_or_create_session()` for true new-session boundaries
- `gateway/run.py`
  - rewires `session_key -> session_id` after compression splits
- `tools/session_search_tool.py`
  - existing parent-chain traversal that already treats compression continuations as one lineage

## Workspace name derivation constraints

Using the lineage root `session_id` as the **mapping key** does **not** mean Hermes can always use the raw string unchanged as the Coder workspace **name**.

### Hermes `session_id` formats today

Hermes currently generates session IDs in two closely related forms:

- agent-created sessions: `YYYYMMDD_HHMMSS_<6 hex chars>`
- gateway-created sessions: `YYYYMMDD_HHMMSS_<8 hex chars>`

So the current raw lengths are:

- 22 characters for the 6-hex form
- 24 characters for the 8-hex form

Both are comfortably short, but they contain underscores.

### Coder workspace naming constraints

Coder's API schema for `CreateWorkspaceRequest` documents these workspace-name rules:

- must start with a letter or number
- may contain only letters, numbers, and hyphens
- may not contain spaces or other special characters
- may not be named `new` or `create`
- maximum length: 32 characters
- must be unique within the user's workspaces

### Implication

A raw Hermes `session_id` is **not** safe to use directly as a Coder workspace name, because the underscore characters violate Coder's documented name constraints.

Length is **not** the blocker here; character set is.

### Practical naming guidance

Hermes should therefore distinguish between:

- **workspace mapping key**: raw lineage root `session_id`
- **workspace name**: a Coder-safe derivative of that key

The workspace name scheme should be fixed as:

- start from the raw lineage root `session_id`
- replace every `_` with `-`
- prepend the stable prefix `hermes-`

In other words:

- **mapping key**: raw lineage root `session_id`
- **workspace name**: ``hermes-`` + `session_id.replace("_", "-")`

Examples:

- root key: `20260521_173045_ab12cd`
- workspace name: `hermes-20260521-173045-ab12cd`
- root key: `20260521_173045_ab12cd34`
- workspace name: `hermes-20260521-173045-ab12cd34`

This scheme remains within Coder's 32-character limit for current Hermes session ID formats:

- 6-hex form -> 29 chars after prefixing and normalization
- 8-hex form -> 31 chars after prefixing and normalization

Using the fixed `hermes-` prefix is desirable because it:

- avoids accidental collision with user-created workspace names
- ensures the generated name can never equal reserved values like `new` or `create`
- makes Hermes-owned workspaces easy to identify operationally
- gives Hermes one deterministic reversible naming rule instead of multiple acceptable variants


Hermes already has a reasonably clean terminal backend abstraction:

1. `tools/environments/base.py`
   - Defines the shared `BaseEnvironment` contract.
   - Handles command wrapping, session snapshotting, cwd tracking, interrupts, stdin handling, and process waiting.

2. `tools/environments/*.py`
   - Backend-specific implementations such as `local`, `ssh`, `docker`, `modal`, `daytona`, and `singularity`.

3. `tools/terminal_tool.py`
   - Parses terminal backend config from env vars.
   - Selects the backend in `_create_environment()`.
   - Runs backend readiness checks in `check_terminal_requirements()`.

4. `tools/file_tools.py` and `tools/code_execution_tool.py`
   - Reuse the same backend selection and environment lifecycle.
   - Contain backend-specific branching that must stay aligned with `terminal_tool.py`.

5. CLI / gateway config surfaces
   - `hermes_cli/config.py`
   - `hermes_cli/setup.py`
   - `hermes_cli/web_server.py`
   - `gateway/run.py`

This means adding `coder` is mostly an integration exercise across several layers, not just a single environment class.

---

## Key Assumption To Validate First

The biggest implementation fork depends on **how Hermes will talk to Coder**.

There are two realistic models:

### Option A: Coder CLI-backed backend (recommended v1)

Use the official `coder` CLI as the transport layer, with Hermes implementing a dedicated `coder` backend that shells out to commands such as:

- `coder ssh <workspace> -- bash -lc '<wrapped command>'`
- or the equivalent non-interactive shell form Hermes needs for `_run_bash(...)`

This approach intentionally reuses Coder's supported execution path rather than re-implementing its agent connectivity stack in Python. Source analysis shows `coder_workspace_bash` is functionally equivalent to `coder ssh <workspace> <command>` and ultimately executes through the workspace agent's built-in SSH server after Coder resolves the workspace, auto-starts it if needed, and waits for the agent to become ready.

Implications:

- fastest path to a working Hermes `coder` backend
- lowest implementation and maintenance risk
- inherits Coder's existing auth, workspace resolution, auto-start, and readiness behavior
- avoids re-implementing Coder's tailnet / coordinate / agent dialing stack in Hermes
- keeps Hermes aligned with its current environment abstraction: one backend object that executes wrapped shell commands and returns stdout/stderr/exit status

Trade-offs:

- requires the user to have the `coder` CLI installed locally
- requires the CLI to already be authenticated and able to reach the target workspace
- process/session behavior is only as rich as what Hermes can layer on top of `coder ssh`

Recommended v1 shape:

- add a dedicated `CoderEnvironment` instead of pretending Coder is just generic SSH
- let Coder own workspace lifecycle/connectivity concerns
- let Hermes own command wrapping, cwd/session snapshots, process supervision, and tool integration

### Option B: Coder is reached through the Coder HTTP/WebSocket API (`/pty`)

This option implements `CoderEnvironment` without depending on the local `coder` CLI. Instead, Hermes talks directly to Coder's public APIs and uses the workspace agent PTY endpoint as the execution transport.

Decision for this option:

- use `GET /api/v2/workspaceagents/{workspaceagent}/pty` as the command-execution transport
- use normal Coder API-key / session-token authentication semantics rather than requiring `coder` CLI login state
- ignore `container` and `container_user` for v1; always execute in the workspace agent's default shell environment
- use a Python terminal-emulation library such as `pyte` to consume PTY output and recover visible screen text for stdout-style reads

Relevant Coder APIs:

- `GET /api/v2/workspaces/{workspace}`: resolve the target workspace and inspect current state
- `POST /api/v2/workspaces/{workspace}/builds`: start the workspace when it is stopped
- `GET /api/v2/workspaces/{workspace}/watch-ws` or equivalent polling: wait until the workspace / agent is ready
- `GET /api/v2/workspaceagents/{workspaceagent}`: resolve the target agent
- `GET /api/v2/workspaceagents/{workspaceagent}/pty`: open the WebSocket PTY transport used for command execution
- `GET /api/v2/workspaceagents/{workspaceagent}/logs`: optional diagnostics when startup or execution fails

Protocol shape:

- client -> server: **binary WebSocket frames carrying JSON payloads** (not text frames)
  - input: `{ "data": "..." }`
  - resize: `{ "height": ..., "width": ... }`
- server -> client: raw PTY byte stream (terminal output, including ANSI control sequences)
- the PTY stream should be parsed into a screen model with `pyte` or equivalent so Hermes can reason about visible text

### Stdin feasibility result (validated)

Hermes validated protocol-level stdin on a real Coder workspace using the configured `hermes-dev` profile credentials.

Observed behavior from the probe:

- sending JSON as **text frames** fails with close code `1003` (`unexpected frame type ... expected MessageBinary`)
- sending raw binary bytes without JSON control framing does not produce usable stdin semantics
- sending JSON payloads as **binary frames** works:
  - `{ "data": "<stdin payload>" }` is consumed by the remote command
  - `{ "data": "\u0004" }` (Ctrl+D / EOT) acts as EOF for stdin-driven commands
  - command exits normally and shell-level exit marker returns `0`

Practical implication for `CoderEnvironment` stdin support:

- do **not** rely on heredoc for Coder stdin as the primary path
- implement stdin by writing protocol input frames over the PTY websocket:
  1. open PTY websocket with wrapped command
  2. send stdin payload in one or more binary JSON `data` frames
  3. send EOF via binary JSON `{"data":"\u0004"}`
  4. continue receiving output until exit marker is observed / stream closes

Notes:

- chunk large stdin payloads to avoid websocket message-size limits
- keep shell-level exit marker parsing (already used for PTY exit code recovery)

Implications for Hermes:

- this is not a structured exec API; it is a terminal stream API
- stdout/stderr are not natively separated by the transport, which is acceptable for Hermes terminal-backend purposes in v1
- completion also does not need to be treated as a first-class transport capability for v1; Hermes can operate without a special completion protocol
- exit code is the main semantic gap to validate during implementation; first verify whether PTY EOF / connection close is sufficient in the practical one-shot cases Hermes cares about, and only add extra shell-level markers if that proves necessary
- Hermes still needs to honor caller-specified cwd even if the PTY transport does not natively model it; the fallback approach is to prefix commands with `cd <cwd> && ...`, so cwd support is not considered a blocker
- compared with Option A, this removes the local CLI dependency but increases backend-side protocol and shell-semantic complexity

Recommended v1 shape for Option B:

- add a dedicated `CoderEnvironment` that manages a PTY-backed session per Hermes environment session
- maintain a parsed screen buffer using `pyte` for readable output extraction
- start with non-container, non-interactive command execution semantics that are compatible with Hermes `terminal`, `file_tools`, and `execute_code`
- do not attempt stdout/stderr separation in v1
- do not require a dedicated completion protocol in v1
- treat exit-code capture as the main behavior to validate during development, with fallback to shell wrapping only if EOF-based detection is insufficient
- implement cwd by command prefixing (`cd <cwd> && ...`) when needed
- treat richer interactive PTY UX as a follow-up enhancement rather than a v1 requirement

---

## Proposed Rollout Strategy

Implement the backend in three phases.

### Phase 1: Minimal terminal execution path

Goal: get `terminal` working reliably against a configured Coder workspace.

Deliverables:

- new `CoderEnvironment`
- `terminal.backend: coder` recognized by config and runtime
- `check_terminal_requirements()` support
- basic execution, cwd persistence, snapshot behavior, cleanup behavior

Non-goals for this phase:

- full UX polish
- deep setup wizard ergonomics
- advanced file sync optimization

### Phase 2: Full toolchain compatibility

Goal: ensure all tools that rely on terminal environments work correctly with Coder.

Deliverables:

- `file_tools` compatibility
- `execute_code` compatibility
- background `process` compatibility
- clear handling of temp files / workspace-local artifacts / file paths

### Phase 3: UX, docs, and hardening

Goal: make the backend feel first-class rather than experimental.

Deliverables:

- setup flow support
- dashboard config support
- docs and tips refresh
- integration tests or a validated manual test path
- error messages that explain missing Coder configuration clearly

---

## Proposed Code Changes By Area

## 1. New backend implementation

### Primary file

- **Create:** `tools/environments/coder.py`

### Responsibilities

This module should define `CoderEnvironment`, a `BaseEnvironment` subclass.

At minimum it must support:

- backend construction from resolved config
- `_run_bash(...)`
- `cleanup()`
- any backend-specific lifecycle bootstrap needed before `init_session()`

Depending on the chosen transport, it may also need:

- `_before_execute()` for file sync or workspace liveness checks
- temp directory override via `get_temp_dir()` if `/tmp` is not the correct runtime location
- interrupt/cancel plumbing if execution is API-driven

### Reference implementations

Use these existing backends as templates:

- `tools/environments/ssh.py`
  - best reference if Coder access is SSH-based
- `tools/environments/daytona.py`
  - best reference if Coder access is SDK/API-based
- `tools/environments/base.py`
  - source of truth for required environment behavior

### Design objective

Keep `CoderEnvironment` aligned with the existing abstraction rather than inventing a parallel execution model. The more it behaves like the other backends, the less incidental work will be needed elsewhere.

---

## 2. Terminal backend factory and config parsing

### Primary file

- **Modify:** `tools/terminal_tool.py`

### Required updates

#### A. Extend config parsing in `_get_env_config()`

Add Coder-specific config inputs.

Exact fields depend on the chosen transport, but likely candidates include:

- Coder base URL
- workspace identifier or workspace name
- agent / username / owner context
- auth token
- workspace path / cwd override
- optional transport details if SSH is used under the hood

The result should be a single normalized config shape that downstream code can consume.

#### B. Extend backend factory in `_create_environment()`

Add a `coder` branch that constructs `CoderEnvironment`.

This is the main runtime entry point for the backend and must stay authoritative.

#### C. Extend readiness validation in `check_terminal_requirements()`

Add explicit validation for the Coder backend.

The check should answer:

- is the backend configured enough to start?
- are required credentials present?
- is the transport dependency available?
- can Hermes produce a useful error if not?

This should fail fast with actionable logging, similar to the current `ssh` and `modal` checks.

#### D. Refresh terminal backend documentation strings

Update any terminal backend lists and help text in this file so they mention `coder`.

---

## 3. Shared environment reuse for file tools

### Primary file

- **Modify:** `tools/file_tools.py`

### Why this matters

Hermes file tools do not operate independently of the terminal backend. They create or reuse the same environment instance. Today this file contains backend branching for:

- image-based backends
- ssh-specific config
- local-specific config
- environment creation through `_create_environment()`

### Required updates

- add Coder-aware branching where environment config is assembled
- pass Coder-specific config into `_create_environment()`
- ensure any assumptions that only `ssh` is the remote-machine backend are removed

### Design note

This file currently duplicates backend selection logic found elsewhere. If implementation reveals significant duplication for `coder`, consider a follow-up refactor to centralize environment-construction inputs, but do not block v1 on that refactor.

---

## 4. Shared environment reuse for execute_code

### Primary file

- **Modify:** `tools/code_execution_tool.py`

### Why this matters

`execute_code` reuses the terminal backend and currently mirrors much of the backend branching in `file_tools.py`.

### Required updates

- add Coder-aware environment construction
- ensure temp file placement works inside the Coder workspace/runtime
- ensure the remote execution path behaves correctly when Python is invoked inside the Coder environment

### Important validation questions

- Does the Coder workspace have Python available by default?
- If not, should that be a documented prerequisite or a runtime error?
- Are temp directories and file write locations stable across sessions?

---

## 5. Config schema and environment bridging

### Files to modify

- `hermes_cli/config.py`
- `gateway/run.py`

### `hermes_cli/config.py`

Add default configuration entries for the Coder backend.

This should define:

- backend selection support
- any new terminal config keys for Coder
- sane defaults where possible

The config should make it obvious which values are required vs optional.

### `gateway/run.py`

Bridge config.yaml terminal settings into runtime env vars for the gateway path.

Current code already maps existing terminal config keys to env vars like:

- `backend -> TERMINAL_ENV`
- `ssh_host -> TERMINAL_SSH_HOST`
- `daytona_image -> TERMINAL_DAYTONA_IMAGE`

Add equivalent bridging for Coder keys so Discord/Telegram/gateway sessions can use the backend without special-case setup outside normal config.

---

## 6. CLI and dashboard configuration surfaces

### Files to modify

- `hermes_cli/setup.py`
- `hermes_cli/web_server.py`

### `hermes_cli/setup.py`

Add `coder` to the terminal backend selection menu and provide a lightweight setup flow for the required settings.

The setup flow should prioritize:

- collecting the minimum viable config
- validating obvious mistakes
- not over-designing v1

The CLI setup does not need to solve every operational detail, but it should at least make the backend discoverable and configurable.

### `hermes_cli/web_server.py`

Add `coder` to the `terminal.backend` options list.

If the web config schema exposes individual terminal settings, add the Coder-specific fields there as well.

---

## 7. Documentation and user-facing strings

### Files to modify

- `tools/environments/__init__.py`
- `tools/terminal_tool.py` (docs/help text)
- `hermes_cli/tips.py`
- any terminal backend documentation pages that enumerate supported backends

### Why this matters

The project currently contains multiple places that explicitly say Hermes supports six terminal backends. Those references will become stale immediately after the feature lands.

### Expected updates

- add `coder` to supported backend lists
- update wording where backend behavior assumptions are now outdated
- ensure examples remain coherent if Coder is not container-based

---

## 8. Tests and validation strategy

## Minimum automated test coverage

### A. Requirements checks

- **Modify:** `tests/tools/test_terminal_requirements.py`

Add coverage for:

- `TERMINAL_ENV=coder` with missing required config -> returns `False` with a clear error
- `TERMINAL_ENV=coder` with mocked valid config -> returns `True`
- unknown backend error text remains accurate after adding `coder`

### B. Config bridge tests

- **Modify:** `tests/gateway/test_config_cwd_bridge.py`

Add coverage for:

- `terminal.backend: coder` bridging to `TERMINAL_ENV`
- Coder-specific config keys bridging to the expected env vars

### C. Backend unit tests

- **Create:** `tests/tools/test_coder_environment.py`

Test the environment class in isolation, using mocks/fakes for the transport.

Suggested areas:

- environment construction
- command wrapping/execution entry path
- cleanup behavior
- cwd persistence assumptions
- interrupt/cancel behavior if supported

### D. Integration or manual validation path

If CI cannot talk to a live Coder deployment, document a reproducible manual validation flow.

If live integration testing is feasible, add:

- **Create:** `tests/integration/test_coder_terminal.py`

This should mirror the existing backend-specific integration tests in spirit, not necessarily in exact structure.

---

## Recommended Implementation Order

1. **Finalize the Coder transport model**
   - SSH-backed wrapper vs API/SDK-backed exec

2. **Build `CoderEnvironment`**
   - get construction, `_run_bash()`, and cleanup working

3. **Wire `coder` into `tools/terminal_tool.py`**
   - config parsing
   - factory selection
   - requirements check

4. **Validate plain terminal execution manually**
   - simple commands
   - cwd persistence
   - repeated commands across the same environment

5. **Wire `coder` into `file_tools.py`**
   - ensure file operations attach to the same environment

6. **Wire `coder` into `code_execution_tool.py`**
   - ensure Python execution flow works correctly

7. **Add config bridging and setup/UI support**
   - `config.py`, `gateway/run.py`, `setup.py`, `web_server.py`

8. **Add tests and update docs**
   - unit tests first
   - integration/manual validation docs second

9. **Run targeted verification**
   - automated tests
   - smoke test against a real Coder workspace

---

## File Change Inventory

## New files

- `tools/environments/coder.py`
- `tests/tools/test_coder_environment.py`
- optionally `tests/integration/test_coder_terminal.py`

## Existing files likely to change

- `tools/terminal_tool.py`
- `tools/file_tools.py`
- `tools/code_execution_tool.py`
- `hermes_cli/config.py`
- `gateway/run.py`
- `hermes_cli/setup.py`
- `hermes_cli/web_server.py`
- `tools/environments/__init__.py`
- `hermes_cli/tips.py`
- `tests/tools/test_terminal_requirements.py`
- `tests/gateway/test_config_cwd_bridge.py`

---

## Risks and Tradeoffs

## 1. Transport mismatch risk

If the design assumes SSH semantics but the real Coder integration is API-driven, implementation may need to be partially redone.

**Mitigation:** explicitly decide the transport model before coding the backend class.

## 2. Duplicate backend branching risk

Backend-specific conditionals currently exist in multiple files, especially `terminal_tool.py`, `file_tools.py`, and `code_execution_tool.py`.

**Mitigation:** keep the first pass minimal and consistent; consider a follow-up refactor only after Coder works end-to-end.

## 3. File path semantics risk

Coder workspaces may not behave like local, container, or SSH targets in exactly the same way.

Potential issues:

- cwd defaults
- temp file location
- absolute vs workspace-relative paths
- artifact paths for generated files

**Mitigation:** validate file and temp path behavior early, not after terminal execution already works.

## 4. Background process risk

If the Coder transport does not support long-lived process handles well, the `process` tool may need special handling or explicit scope constraints for v1.

**Mitigation:** define whether background process support is in-scope for the first version and test it deliberately.

## 5. Auth and workspace lifecycle risk

Depending on how Coder authentication and workspace lookup work, failures may occur before a shell is even started.

**Mitigation:** put clear validation into `check_terminal_requirements()` and return actionable error messages.

---

## Open Questions

These should be answered before implementation starts in earnest:

1. **What exact Coder transport is in scope?**
   - direct SSH
   - CLI wrapper
   - REST API / SDK exec

2. **How is a workspace identified?**
   - workspace name
   - workspace ID
   - owner/workspace tuple
   - project path

3. **What credentials are required?**
   - API token
   - SSH key
   - session cookie / CLI auth

4. **What is the default working directory inside a Coder workspace?**
   - workspace root
   - user home
   - repository checkout path

5. **Should v1 support background processes fully?**
   - yes, parity with other backends
   - no, terminal/file/execute_code only for first release

6. **Should Hermes sync files into Coder, or assume the target repo already exists in the workspace?**
   - sync model
   - workspace-native model

7. **Is Coder-specific setup meant to be first-class in the interactive setup flow now, or acceptable as a mostly manual advanced configuration in v1?**

---

## Acceptance Criteria

The implementation should be considered complete when all of the following are true:

- `terminal.backend: coder` is accepted by Hermes config surfaces
- Hermes can construct a `CoderEnvironment` and execute shell commands in the target workspace
- repeated commands preserve expected session behavior through the existing `BaseEnvironment` flow
- `terminal`, `file`, and `execute_code` all function against the Coder workspace
- runtime config can flow from config.yaml/gateway config into the backend without ad hoc setup
- targeted automated tests cover requirements and config bridging
- user-facing backend lists and docs are updated to mention Coder
- the feature has been smoke-tested against a real Coder workspace or documented with a reproducible manual validation flow

---

## Suggested Validation Checklist

After implementation, validate in this order:

1. Configure `terminal.backend: coder`
2. Run a simple command: `pwd`
3. Run a second command: `cd ... && pwd` or equivalent to verify cwd persistence
4. Create a file through terminal
5. Read that file through `read_file`
6. Modify it through `patch` or `write_file`
7. Run a simple Python script through `execute_code`
8. If in scope, validate a background process via `process`
9. Restart a fresh session and confirm the configured backend still resolves correctly

---

## Final Recommendation

For a first implementation, optimize for **architectural fit and end-to-end compatibility**, not feature completeness.

The best v1 is:

- one well-behaved `CoderEnvironment`
- fully wired into the existing environment factory/config flow
- compatible with terminal/file/execute_code
- covered by focused tests

If deeper Coder-specific ergonomics are needed later, they can be layered on after the backend is stable.
