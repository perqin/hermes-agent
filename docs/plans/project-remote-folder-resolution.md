# Backend-aware Project Folder Resolution Implementation Plan

> **For Hermes:** Implement this plan task-by-task, keeping each commit scoped and preserving the local-backend contract.

**Goal:** Make Project folder paths canonical in their terminal-backend filesystem namespace and carry that namespace contract through Project-bound Kanban boards: preserve current local behavior, resolve non-local paths in the terminal environment, and require every assigned board worker profile to be able to access the same backend path.

**Architecture:** Add one Project path resolver above `projects_db`. It acquires the configured terminal environment through the existing terminal runtime/factory, classifies filesystem locality from the environment capability (`env.is_local`) rather than a backend-name list, and either applies the existing host-local normalization or performs a bounded backend-side `cd` + `pwd -P`. All externally reachable Project folder mutations use this resolver before entering a database transaction; `projects_db` receives an explicit “already canonical” signal so a controller OS cannot reinterpret a remote path. A lightweight provider-level tri-state exposes the same locality contract to frontends without starting a sandbox: Desktop combines it with connection locality to choose native picker, gateway-host picker, or text entry, while submit-time resolution remains authoritative. Binding a Project to a Kanban board snapshots the Project's canonical primary path as the board workspace contract. The dispatcher resolves and materializes a task workspace through the assignee profile's terminal environment; successful backend-side access is the proof that the profile satisfies the board contract.

**Tech stack:** Python, argparse, SQLite, terminal `BaseEnvironment`/plugin providers, TUI/Desktop JSON-RPC, React/TypeScript nanostores, pytest, Vitest.

---

## 1. Scope and invariants

### In scope

- `hermes project create ... <folders>` and `--primary PATH`.
- `hermes project add-folder <project> <path>`.
- `hermes project remove-folder` and `set-primary`, so a canonical remote path can later be referenced without being reinterpreted by the controller OS.
- `projects.create`, `projects.add_folder`, `projects.remove_folder`, and `projects.set_primary` JSON-RPC methods used by Desktop/TUI/dashboard.
- The `desktop_project` agent tool's `create` action.
- Profile-correct terminal configuration, credentials, environment cache identity, and error propagation for project RPCs.
- A provider-neutral, profile-scoped filesystem-locality capability for Project path-entry UIs.
- Desktop picker-versus-text behavior across native, remote-gateway, and terminal-non-local cases, including every create/add/open entrypoint.
- Immediate Desktop reconciliation to the canonical path returned by the backend.
- Web/Kanban dashboard Project-directory validation, error handling, canonical response state, and translated helper copy.
- Project ownership/session-workspace consumers that would otherwise reinterpret or reject the newly canonical remote path on the controller host.
- Project-bound Kanban board metadata, task workspace derivation, backend-side worktree materialization, and worker startup for a shared backend path.

### Required behavior

1. **Local filesystem:** preserve `_normalize_path` semantics exactly: trim, host `expanduser`, host `abspath`, remove trailing separators. Do not add existence, directory, accessibility, repository, or symlink checks. In particular, local missing paths and regular files remain accepted; quoted-empty behavior must be characterized before deciding whether to change it separately.
2. **Non-local filesystem:** resolve on the selected environment, never with controller-side `Path`, `os.path.abspath`, `expanduser`, `exists`, or `isdir`.
3. **Classification:** use the acquired environment's locality capability. `env.is_local is True` is local; false or absent is treated as non-local. Project code must not contain a backend-name enum or `if backend == ...` ladder.
4. **Resolution:** run a shell-safe, bounded equivalent of `(cd -- <raw-path> && pwd -P)` through `env.execute()`. Persist only the single absolute physical path returned by the backend.
5. **Failure:** environment creation failure, timeout, non-zero command result, missing/ambiguous output, or non-absolute output raises a sanitized `ValueError` before any Project database write.
6. **Atomic create:** resolve every supplied folder and primary path first; if any one fails, create no project and write no folder rows.
7. **No session-cwd side effect:** resolving a folder must not adopt it as the caller's durable terminal cwd. Use an internal environment operation, not the model-facing `terminal_tool()` result/finalizer, and keep the probe in a subshell or otherwise avoid recording cwd state.
8. **Profile isolation:** a Project RPC targeting profile B must use B's `HERMES_HOME`, terminal policy, terminal-provider registration scope, and credential/secret scope. It must not reuse profile A's cached SSH/container/plugin environment.
9. **Existing rows:** no schema migration and no eager rewriting. Previously stored paths remain byte-preserved until the user explicitly mutates them.
10. **Compatibility:** labels, primary-folder promotion/demotion, duplicate `INSERT OR IGNORE`, active-project behavior, archived-project behavior, and RPC method/result shapes stay unchanged.
11. **RPC responsiveness:** every Project mutation that may initialize or execute against a remote environment runs through the existing long-handler executor rather than blocking the JSON-RPC reader thread.
12. **Canonical-path consumption:** once a remote path has been validated, session/project ownership code must compare and propagate it without controller-side `isdir`, `abspath`, `normcase`, or `os.sep` assumptions.
13. **Board binding contract:** a bound board's `default_workdir` is the Project's canonical primary path in the Project owner's backend filesystem. It is not translated to a controller-host path.
14. **Worker reachability contract:** every profile assigned work on that board must configure a terminal environment that can access the same canonical path. At dispatch, backend-side `cd`/canonicalization must prove reachability before worktree creation or worker execution; failure blocks the task with an actionable profile/path error.
15. **Backend-side materialization:** local assignees retain the existing controller-local `Path`/Git/Popen flow. Non-local assignees create, inspect, and reuse Project worktrees through their terminal environment; the dispatcher must never run controller-local Git against a backend-owned path.
16. **No backend identity enum:** the board need not persist a built-in backend name. Environment capability plus successful access to the canonical board path is the runtime compatibility check. Profiles are operationally required to target the same durable/shared backend filesystem.
17. **Workspace authority:** the task-derived Project/board workspace overrides a profile's default `terminal.cwd`; the profile still supplies backend, image, mount, network, resource, credentials, and model/tool policy.
18. **Frontend locality is profile-scoped:** UI path-entry mode is derived from the active profile's terminal-filesystem capability, not from Desktop connection mode and not from a backend-name list.
19. **Desktop input modes:** when the active profile's terminal filesystem is local, retain the existing picker appropriate to the connection (native Electron picker for a local connection, in-app gateway-host browser for a remote connection). When the terminal filesystem is non-local or capability is unknown, show a plain path input and never browse the controller/gateway filesystem.
20. **Submit is authoritative:** frontends may trim only for empty-value UX; they do not normalize, expand, stat, or pre-canonicalize Project paths. `projects.create`/`projects.add_folder` resolve and validate through the terminal environment, keep the dialog/input on failure, and return the canonical stored path on success.

### Non-goals

- Persisting backend names, sandbox IDs, mount mappings, or provider descriptors in `projects.db`.
- Migrating a Project automatically when `terminal.backend` changes.
- Requiring a Project folder to be a Git repository.
- Adding a Project-specific terminal backend manager or duplicating terminal configuration/factory logic.
- Changing repository discovery or making local Project creation stricter.
- Proving backend equality by comparing provider names or serialized provider configuration. Reachability of the canonical bound path is the contract; providers remain free to expose the same filesystem through different configurations.
- Making arbitrary unbound Kanban `dir`/`worktree` paths backend-aware. This plan covers paths derived from a Project-bound board; broader Kanban workspace transport can reuse the resulting mechanism later.
- Automatically migrating existing materialized task workspaces when a Project primary path changes. Existing tasks retain their workspace snapshot; safe rebinding remains a separate explicit workflow.

---

## 2. Current seams to preserve and reuse

- CLI parser and dispatch: `hermes_cli/projects_cmd.py:12-75`; folder handlers at `:133-190`.
- Per-profile persistence and current host normalization: `hermes_cli/projects_db.py:23-25`, `:108-111`, `:227-263`, `:312-366`.
- Environment capability: `tools/environments/base.py:142-169`; only `LocalEnvironment` sets `is_local = True` at `tools/environments/local.py:689-694`.
- Unified execution result: `BaseEnvironment.execute()` returns `output`/`returncode` at `tools/environments/base.py:475-565`; its wrapper already emits physical `pwd -P` cwd markers via `tools/environments/base_session_env.py:43-46`.
- Canonical environment creation/cache lifecycle: `_plan_execution`, `_acquire_env`, and `_create_configured_env` in `tools/terminal_tool.py:895-1047` and `tools/terminal_tool_lifecycle.py:66-81`.
- Plugin environment creation and provider capability metadata: `tools/terminal_tool_backends.py:183-220` and `agent/terminal_env_provider.py:20-103`.
- Profile-scoped Project RPCs: `tui_gateway/methods_projects.py:27-99`; current generic wrapper only binds `HERMES_HOME` in `tui_gateway/server.py:494-523`.
- Project mutation RPCs are not currently in `_LONG_HANDLERS` (`tui_gateway/server.py:151-177`), although environment creation and `env.execute()` are synchronous and may cold-start SSH/cloud/container runtimes.
- Agent Project creation: `tools/project_tools.py:81-106` currently performs host-side `abspath`/`expanduser` itself.
- The Project workspace callback still performs controller-side `abspath`/`isdir` and silently returns for remote-only paths (`tui_gateway/agent_callbacks.py:129-155`); `tools/project_tools.py:33-39` also swallows callback failures.
- Project ownership still normalizes with controller `os.path`/`os.sep` in `hermes_cli/projects_db.py:469-484`; the pure path-style-aware comparison model already exists in `tui_gateway/project_tree.py:54-73` and should be moved/reused at a lower layer rather than imported upward from the gateway.
- Desktop create/add-folder cache updates: `apps/desktop/src/store/projects.ts:873-953` and `:1029-1070`.
- Desktop currently chooses local native picker versus gateway-host in-app picker from connection mode (`apps/desktop/src/lib/desktop-fs.ts:210-224`), not from the active profile's terminal backend. `ProjectDialog` always invokes that picker (`apps/desktop/src/app/chat/sidebar/project-dialog.tsx:113-133,207-258,310-315`), so a locally connected Docker/SSH/plugin profile browses the wrong filesystem.
- `openFolderAsProject()` and all of its menu/keybinding/palette entrypoints also call the same picker (`apps/desktop/src/store/projects.ts:1363-1419`); they must use the same profile-aware path-entry policy rather than retaining a hidden native-picker path.
- The React Web dashboard has no standalone Project-management page; `/chat` embeds the real TUI. Its relevant structured frontend is the Kanban dashboard plugin. That plugin already uses text inputs for Board project directories (`plugins/kanban/dashboard/dist/index.js:2193-2278,2317-2392`), but `plugin_api.py:_validate_workdir` validates with controller `Path.expanduser/is_dir/resolve` (`:1369-1416`) and therefore rejects or rewrites backend-owned paths.

---

## 3. Implementation tasks

### Task 1: Expose a strict internal terminal-environment acquisition seam

**Objective:** Let non-tool code obtain the same configured/cached environment used by terminal and file tools without executing a model-facing command or inspecting backend names.

**Files:**

- Modify: `tools/terminal_tool.py`
- Modify if factoring is needed: `tools/terminal_tool_lifecycle.py`
- Modify: `agent/terminal_env_provider.py`
- Modify: `tools/terminal_tool_backends.py`
- Test: `tests/tools/test_terminal_tool.py` or the nearest existing environment-lifecycle test file
- Test: `tests/agent/test_terminal_env_registry.py`

**Steps:**

1. Extract or expose an internal `acquire_terminal_environment(...)` operation around the existing planning/cache path. It must reuse `_get_env_config`, `_select_image`, `_resolve_task_host_cwd`, `_create_configured_env`, creation locks, `_active_environments`, activity timestamps, and cleanup registration rather than creating a second cache.
2. Accept the real session `task_id` when available. Also accept an explicit operation/cache scope for sessionless Project RPCs, so two profiles in one TUI/Desktop backend cannot collapse onto the same `default` remote environment.
3. Keep failures strict for this API. Unlike `ensure_task_env()`, do not return `None` for both “local” and “remote creation failed,” and do not swallow creation errors. Convert internal `_Rejected`/connection/provider failures into a caller-consumable exception with redacted text.
4. Return the environment object itself; the Project layer reads `getattr(env, "is_local", False)`. Do not return or expose a hard-coded backend classification.
5. Clarify in `TerminalEnvironmentProvider.create_environment()` documentation that returned environments participate in the `is_local` capability contract. Preserve an environment's explicit value; for duck-typed plugin environments that omit it, stamp a value derived from the provider's existing `is_remote` capability, with failures defaulting safely to non-local.
6. Add one contract test covering a plugin backend not named in core: acquisition returns its environment, its remote/local classification comes from provider/environment capability, and no backend enumeration is required.

**Acceptance:** A local built-in, a non-local built-in-shaped fake, and a plugin provider can all be acquired through one API; remote acquisition errors are distinguishable from local selection; profile-qualified operation scopes do not share cached environments accidentally.

### Task 2: Add the single Project folder resolver

**Objective:** Centralize local compatibility and backend-owned remote canonicalization.

**Files:**

- Create: `hermes_cli/project_paths.py`
- Test: `tests/hermes_cli/test_project_paths.py`

**Steps:**

1. Move or expose the current local lexical normalization without changing its behavior. Keep it as the only local branch.
2. Implement `resolve_project_folder(path, *, task_id=None, operation_scope=None)`:
   - acquire the configured terminal environment through Task 1;
   - if `env.is_local is True`, return the existing local normalization result without executing or statting the path;
   - otherwise execute a backend-side, shell-safe subshell equivalent of `(builtin cd -- <path> && pwd -P)` so the shared environment's cwd is not changed;
   - preserve backend-side `~` expansion without allowing command injection;
   - use a bounded timeout and disable compound-background rewriting for this internal probe;
   - frame the result with a unique marker so login-shell/banner noise cannot be mistaken for the path;
   - require the environment's `returncode` to be zero and exactly one usable absolute marked result; do not depend on `result["cwd"]`, because duck-typed/managed providers only guarantee `output` and `returncode`;
   - never feed the result back through controller `os.path` APIs.
3. Add a batch helper for Project creation. Deduplicate by the resolved canonical strings, ensure `primary_path` is resolved only once, and return all results only after every path succeeds.
4. Add a reference resolver for `remove-folder`/`set-primary`:
   - first accept an exact path already stored on the project, allowing removal of a directory that has since been deleted;
   - otherwise apply local normalization or remote resolution and match the resulting canonical path;
   - never make stale remote folders impossible to remove.
5. Raise stable, sanitized `ValueError` messages that identify the input path and whether resolution failed, but do not expose provider exception text, credentials, shell snapshots, or arbitrary remote stderr.
6. Write two behavior-focused tests rather than source-shape tests:
   - a table-driven local/remote resolver contract covering relative path, `~`, dot segments, trailing separators, missing directory, regular file, and a shell-metacharacter path;
   - a failure/atomicity contract covering environment failure, timeout/non-zero result, malformed output, and exact removal of a stale stored remote path.

**Acceptance:** Local results and permissiveness are unchanged. Remote aliases resolve to the backend's physical absolute path, invalid directories fail, and no remote input is interpreted by the controller OS.

### Task 3: Make the database boundary explicit about canonical paths

**Objective:** Prevent `projects_db` from re-normalizing a remote POSIX path with the controller's OS rules while preserving existing direct/local callers.

**Files:**

- Modify: `hermes_cli/projects_db.py`
- Test: `tests/hermes_cli/test_projects_db.py`

**Steps:**

1. Add an explicit keyword contract such as `canonical_paths=False` to path-bearing persistence methods (`create_project`, `add_folder`, `remove_folder`, `set_primary`, and `find_by_primary_path`). Default false retains the existing host-local normalization for callers not yet migrated.
2. When canonical is true, perform only storage-level validation: string/non-empty shape and root-safe trailing-separator cleanup. Do not run `abspath`, `expanduser`, `realpath`, `normcase`, existence checks, or platform-native path parsing.
3. Keep the schema unchanged. Continue using `(project_id, path)` as the folder primary key and preserve `INSERT OR IGNORE`, label updates, first-folder primary selection, and explicit primary promotion.
4. Compare canonical remote primary paths exactly. Keep local case/separator equivalence in the local resolver/comparison path rather than applying host `normcase` to remote strings.
5. Move the path-style-aware segment/key logic currently embedded in `tui_gateway/project_tree.py` into a dependency-safe Project path utility and reuse it from both tree building and `project_for_path()`. Canonical POSIX and Windows-shaped paths must be compared according to their own syntax rather than the controller's `os.sep`/`normcase`.
6. Ensure Project creation receives a completely resolved set before opening `write_txn`, so no partial project or folder row survives a failed batch.
7. Add one cross-platform-oriented database test showing that a canonical POSIX remote path is stored byte-for-byte, remains matchable by `project_for_path`, and is not prefixed/reformatted by the host OS. Keep existing local normalization and duplicate-primary tests green.

**Acceptance:** The DB remains backend-agnostic, but callers can prove a path is already canonical and avoid host reinterpretation; no migration is required.

### Task 4: Route every CLI folder mutation through the resolver

**Objective:** Make `hermes project` honor the selected terminal filesystem while retaining existing exit-code/error conventions.

**Files:**

- Modify: `hermes_cli/projects_cmd.py`
- Test: `tests/hermes_cli/test_projects_cli.py`

**Steps:**

1. Resolve all `create` folders and `--primary` before calling `pdb.create_project(..., canonical_paths=True)`.
2. Resolve `add-folder` before `pdb.add_folder(..., canonical_paths=True)`.
3. For `remove-folder` and `set-primary`, use the exact-first reference policy from Task 2, then pass the canonical path to the DB.
4. Keep `_db_command`'s `ValueError` mapping: print `project: <message>` and return exit code 2. Project-not-found behavior remains exit code 1.
5. Print the canonical stored path in success output, not the raw input spelling.
6. Add one CLI integration test using a fake non-local environment: a relative path becomes the fake backend's absolute path; a failed `cd` exits 2 and leaves the project unchanged. Existing local create/list/show behavior remains the regression check.

**Acceptance:** The real argparse-to-DB command path is exercised, and a failed remote resolution produces no database mutation.

### Task 5: Make Project JSON-RPC mutations profile- and environment-correct

**Objective:** Apply the same resolver to Desktop/TUI/dashboard without crossing profile or environment identities.

**Files:**

- Modify: `tui_gateway/methods_projects.py`
- Modify: `tui_gateway/server.py`
- Test: `tests/tui_gateway/test_projects_rpc.py`

**Steps:**

1. Wrap path-bearing Project mutators in a bounded Project runtime scope that includes:
   - the existing requested-profile `HERMES_HOME` override;
   - that profile's secret scope for provider/SSH credentials;
   - that profile's complete terminal policy via `install_profile_terminal_scope`;
   - a deterministic profile-qualified operation scope for terminal environment caching.
2. Keep this scope narrow to Project mutations unless a generic full profile-runtime helper already exists. Do not silently change unrelated read-only RPC behavior.
3. Resolve `projects.create` and `projects.add_folder` before database writes; apply the exact-first reference policy to remove/set-primary.
4. Continue returning the refreshed Project object. Preserve JSON-RPC error code `5063` for `ValueError`; do not collapse resolution failures into generic `5061`.
5. Add `projects.create`, `projects.add_folder`, `projects.remove_folder`, and `projects.set_primary` to `_LONG_HANDLERS`. Even exact-reference operations are cheap, but keeping all path-bearing mutators on the same dispatch class prevents a future resolver fallback from blocking the reader thread.
6. Add one integration test with launch profile A and requested profile B selecting different fake terminal providers. Assert B's provider executes, B's canonical path lands only in B's `projects.db`, A's environment/DB are untouched, the next unscoped request still uses A, and the four mutators are registered as long handlers.

**Acceptance:** A multiplexed backend cannot validate a profile-B path against profile A's host, credentials, policy, or cached environment.

### Task 6: Expose the active profile's Project path-entry capability

**Objective:** Give Desktop and structured Web surfaces a profile-scoped locality fact without teaching them backend names or making UI rendering create a sandbox.

**Files:**

- Modify: `agent/terminal_env_provider.py`
- Modify: `agent/terminal_env_registry.py`
- Modify: `tools/terminal_tool_backends.py`
- Modify: `tui_gateway/methods_projects.py`
- Modify: `apps/desktop/src/types/hermes.ts`
- Test: `tests/agent/test_terminal_env_registry.py`
- Test: `tests/tui_gateway/test_projects_rpc.py`

**Steps:**

1. Add one lightweight terminal-filesystem locality query backed by the same provider/environment capability contract as the resolver. Extend `TerminalEnvironmentProvider` with a declarative `filesystem_local` capability (default false for isolated plugin filesystems), and expose built-in capability through the terminal backend factory/spec layer so Project/UI callers never maintain their own backend-name set. Assert that metadata agrees with the live environment's `env.is_local`. The query must not cold-start SSH/container/cloud environments merely to paint a dialog.
2. Built-in and plugin registrations expose whether their configured environment runs on the controller filesystem. Preserve uncertainty rather than mislabeling it: an unknown/malformed/unavailable provider capability is `unknown`, and the UI treats it like non-local for picker safety. Actual submission still acquires the environment and is authoritative.
3. Add a profile-scoped `projects.capabilities` RPC, or an additive equivalent on the existing Projects payload, returning a provider-neutral tri-state such as `{filesystem_scope: "local" | "non_local" | "unknown"}`. Prefer a dedicated capability response if adding it to `projects.list` would make stale backends or read caches ambiguous.
4. Resolve the fact inside the same requested-profile home/terminal-provider scope used by Project mutations. A request for profile B must never report profile A's capability.
5. Keep the wire provider-neutral. Do not return a frontend decision based on names such as `docker` or `ssh`; the frontend combines `filesystem_scope` with its own connection mode to choose native picker, gateway-host picker, or text entry.
6. In Desktop, cache the result by exact gateway connection plus profile and invalidate/refetch on profile switch, gateway reconnect, or terminal-backend change. While loading, after failure, and against an older backend missing the capability method, default to text entry rather than opening a picker on an unproven filesystem.
7. Add one profile-isolation test with local profile A and plugin-remote profile B, plus a missing-capability compatibility test proving the result is explicitly `unknown` rather than guessed from a backend string.
8. Keep this metadata-only RPC out of `_LONG_HANDLERS`; it must not acquire an environment. The mutation path remains authoritative and long-running. If implementation cannot derive locality without environment creation, drop the metadata optimization, acquire through Task 1, and register the RPC as long-running rather than adding a second backend classification.

**Acceptance:** Every Project path-entry surface can decide whether browsing the controller/gateway filesystem is valid for the exact active profile, while submit-time backend resolution remains the source of truth.

### Task 7: Remove host normalization from the agent Project tool

**Objective:** Keep model-driven Project creation consistent with CLI and RPC paths.

**Files:**

- Modify: `tools/project_tools.py`
- Modify: `tui_gateway/agent_callbacks.py`
- Test: extend the nearest behavioral Project-tool/callback tests; do not add a source-reading test

**Steps:**

1. Remove the direct `os.path.abspath(os.path.expanduser(...))` in `project_create`.
2. Pass the tool call's real `task_id` to the shared Project resolver so it reuses the session's configured environment and isolation.
3. Perform duplicate-primary lookup using the canonical resolved path, then create with `canonical_paths=True`.
4. Make the workspace callback consume the already validated canonical path. Keep the existing `abspath` + `isdir` guard only when that session's actual environment is local; a non-local environment must not stat the path on the controller host.
5. Stop swallowing authoritative workspace callback failures in `_apply_workspace`. Convert them into the tool's existing `success: false` envelope; do not report that the chat moved when it did not. A background task with no provable live GUI session may still create the Project, but must not borrow or move another session by profile guesswork.
6. Preserve the current JSON success/error shape. On success the workspace callback must receive the canonical backend path and the matching task/session identity.
7. Add one behavior test proving a fake non-local session resolves a relative path remotely and moves to that canonical path without host `isdir`; resolution/callback failure must not falsely report a successful workspace move.

**Acceptance:** CLI, RPC, and agent-tool creation persist the same path for the same profile/environment/input.

### Task 8: Remove controller-local assumptions from Project path consumers

**Objective:** Ensure a correctly stored remote folder remains usable for project ownership and session cwd propagation.

**Files:**

- Modify: `hermes_cli/projects_db.py`
- Modify: `tui_gateway/methods_projects.py`
- Modify: `tui_gateway/project_tree.py`
- Modify as required by the exact call chain: `agent/runtime_cwd.py`
- Preserve unless a regression test proves otherwise: `tui_gateway/session_workdir.py`
- Test: `tests/hermes_cli/test_projects_db.py`
- Test: `tests/agent/test_runtime_cwd.py`
- Test: the nearest `tui_gateway` session-cwd/project callback test file

**Steps:**

1. Reuse the path-style-aware comparison utility from Task 3 in `project_for_path`; do not normalize a canonical remote cwd through the controller OS.
2. Audit the Project-created/switch workspace chain for controller `Path.expanduser`, `Path.is_dir`, `os.path.abspath`, and `os.path.isdir` checks. Local environments keep those checks; non-local environments trust only paths already validated by the Project resolver or validate through that environment.
3. Keep `tui_gateway/session_workdir.py`'s existing precedence where an explicit remote session cwd wins over the global configured cwd. Do not redesign or weaken this working rule.
4. Make `projects.for_cwd` canonicalize its candidate in the requested profile's terminal environment before ownership lookup and return that canonical `cwd`. For non-local filesystems, run any Git branch probe through the same environment or omit the optional branch; never call controller-side `_completion_cwd` or `git_probe.branch(remote_path)`.
5. Ensure session status, sidebar grouping, and `_project_info_for_cwd` can match a remote canonical cwd without probing the controller filesystem.
6. Add one end-to-end invariant test: a remote-only alias can be resolved by `projects.for_cwd`, returned as canonical cwd, adopted as the intended session cwd, and matched back to its Project while every controller-local existence/Git probe is set to fail if called.

**Acceptance:** Correct remote storage is not undone by a later host-local check, and local deleted-cwd healing remains unchanged.

### Task 9: Make a Project-bound board carry a self-contained workspace contract

**Objective:** Let a global Board use a per-profile Project path without requiring every assignee profile to duplicate the Project row.

**Files:**

- Modify: `hermes_cli/projects_cmd.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `plugins/kanban/dashboard/plugin_api.py`
- Test: `tests/hermes_cli/test_kanban_project_link.py`
- Test: `tests/plugins/test_kanban_board_project_api.py`

**Steps:**

1. Treat binding as a snapshot operation. Resolve the Project primary path through the Project owner's terminal environment first, then write the canonical value to board `default_workdir`.
2. Persist enough additive board metadata to use the binding without opening an assignee profile's `projects.db`: canonical `project_id`, Project slug/branch prefix, source profile identity, canonical `default_workdir`, and any backend-resolved Git/workspace-kind fact needed by list/task UI. Do not persist a built-in backend name or provider config snapshot.
3. Make CLI `project bind-board` and Dashboard board binding write the same reciprocal metadata. A successful bind must update both the Project's `board_slug` and the Board's binding snapshot; do not keep the current best-effort exception swallowing that can report a one-sided bind.
4. When a board-scoped task omits an explicit Project/workspace, derive its Project identity, deterministic branch, and desired `<default_workdir>/.worktrees/<task-id>` path from this snapshot. The assignee does not need a matching Project row; it needs filesystem reachability.
5. Keep explicit `--project` resolution fail-closed in the creator profile. The board snapshot is inheritance for a bound board, not a fallback that silently substitutes a different Project for an unresolved explicit request.
6. Define rebinding semantics explicitly: rebinding the Board refreshes the snapshot for future tasks; existing materialized task rows are not rewritten.
7. Add one round-trip test proving profile A can bind its Project, a task assigned to profile B inherits the exact canonical root/slug without a Project row in B, and an explicit unresolved Project still fails.

**Acceptance:** Board metadata is the durable bridge from the per-profile Project registry to the global Kanban queue, while worker compatibility is decided by access to the bound path rather than duplicate Project state.

### Task 10: Materialize Project-bound workspaces in the assigned worker environment

**Objective:** Ensure the exact environment used by the assignee validates and creates its Project worktree instead of handing a backend path to controller-local `Path`, Git, or `Popen(cwd=...)`.

**Files:**

- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/kanban_db_workspace.py`
- Modify: `hermes_cli/kanban_db_dispatch.py`
- Create or extend the narrow worker bootstrap module selected during implementation; do not append a large new flow to the `kanban_db.py` facade
- Test: `tests/hermes_cli/test_kanban_project_link.py`
- Test: the existing Kanban worker workspace/dispatch integration test file nearest `_default_spawn`

**Steps:**

1. Represent a Project-bound task workspace as a desired canonical string plus branch/project provenance. Do not convert a non-local path to controller `Path` or join it with controller `os.path`; use the path-style-aware helpers from Task 3.
2. Run the Project-bound workspace preflight inside the assignee worker process after `-p <assignee>` has installed that profile's home, secrets, terminal policy, and provider registry, but before the agent turn starts. This guarantees the check and materialization occur in the exact worker environment instead of a separately created dispatcher sandbox.
3. Acquire that worker's terminal environment through Task 1 and branch only on `env.is_local`:
   - local: retain the established Git worktree materialization behavior;
   - non-local: backend-side `cd`/`pwd -P` must resolve the bound root to the exact canonical `default_workdir`, then backend-side Git commands validate the repo and create/reuse the deterministic worktree and branch.
4. Treat successful canonical root access as the compatibility proof promised by Board binding. Do not compare backend names. A profile whose environment cannot access the root, resolves it to a different canonical path, lacks the expected repository, or cannot materialize the worktree must block the task before agent commands execute.
5. Return/persist the actual canonical worktree path and resolved branch from the worker preflight. Preserve existing task IDs, claim/run records, retry semantics, and branch naming.
6. Make the dispatcher launch the local Hermes worker process from a neutral existing controller directory when the task workspace is backend-owned. Pass the desired workspace/branch/Board pins explicitly; never call `os.path.isdir(remote_path)` or `Popen(cwd=remote_path)`.
7. Preserve workspace authority in the child: the validated task worktree becomes `TERMINAL_CWD` and the session cwd; assignee `terminal.cwd` cannot overwrite it. Backend/image/mount/network/resource settings still come from the assignee profile.
8. Ensure all assignee profiles intended for a bound Board point to a durable/shared environment. Ephemeral environments that cannot observe the bound root naturally fail the same reachability preflight; no provider-specific prohibition is needed.
9. Keep Board/task DB and attachments/log paths on the shared Kanban control plane. Only repository/worktree operations move to the terminal environment; task state updates remain controller-side and auditable.
10. Add one two-profile integration test using two provider registrations backed by the same fake remote filesystem: profile B materializes and runs under profile A's bound Project root. Add the negative pair where B resolves another filesystem or cannot `cd`; assert no controller Git/Popen cwd touches the remote path and the task blocks with the profile/path named.

**Acceptance:** The global dispatcher may be hosted by any profile, but Project-bound workspace creation and use are authoritative in the assignee's terminal environment; every successful worker therefore satisfies the shared-backend assumption.

### Task 11: Make Desktop Project path entry profile-filesystem-aware

**Objective:** Preserve picker UX only when the selected profile actually shares the browsed filesystem; use explicit remote-path entry otherwise, and reconcile every mutation from backend truth.

**Files:**

- Modify: `apps/desktop/src/types/hermes.ts`
- Modify: `apps/desktop/src/store/projects.ts`
- Modify: `apps/desktop/src/lib/desktop-fs.ts`
- Modify: `apps/desktop/src/app/chat/sidebar/project-dialog.tsx`
- Modify: `apps/desktop/src/app/chat/sidebar/index.tsx` or the shared dialog host selected during implementation
- Modify: `apps/desktop/src/i18n/types.ts`
- Modify: `apps/desktop/src/i18n/{en,zh,zh-hant,ja,ru,ar}.ts`
- Test: `apps/desktop/src/store/projects.test.ts`
- Test: `apps/desktop/src/lib/desktop-fs.test.ts`
- Test: `apps/desktop/src/app/chat/sidebar/project-dialog.test.tsx`
- Add/Test if required by the shared entry flow: `apps/desktop/src/app/chat/sidebar/project-path-dialog.tsx` and its test

**Steps:**

1. Model three renderer-side entry modes from two independent facts:
   - `filesystem_local && local Desktop connection`: existing native Electron system picker;
   - `filesystem_local && remote Desktop connection`: existing in-app picker backed by the gateway host `/api/fs`;
   - `!filesystem_local` or unknown/loading capability: plain path text entry.
2. Keep connection locality in `desktop-fs.ts` limited to selecting native versus gateway-host browsing. It must no longer answer whether the active profile's terminal backend owns that filesystem.
3. Load the Task 6 capability for the exact captured gateway/profile when Project UI opens. Disable any browse action while capability is unresolved, and use text mode if lookup fails or the backend predates the capability RPC. Never infer from `local`/`docker`/`ssh` strings.
4. In `ProjectDialog`, retain the existing browse-and-list workflow for filesystem-local profiles. For non-local profiles, render an unnormalized path input plus explicit Add action (Enter is equivalent); support `~`, relative, POSIX, and provider-defined syntax without host-side `path` helpers. Do not derive the Project name with controller-platform basename rules; retain/use the dialog's explicit name field. Creating a Project batches the entered folders and validates all of them atomically only on the final `projects.create` submission; add-folder mode validates on `projects.add_folder` submission.
5. Route `openFolderAsProject()` through the same shared path-acquisition policy. Calls without a supplied path (sidebar/menu/keybinding/fixed palette command) open the picker or reusable text prompt selected by capability; a command-palette item that already supplies a path skips acquisition but still uses backend canonicalization. Do not duplicate locality checks across entrypoints.
6. Do not call the gateway-host `/api/fs` list/default-cwd endpoints for a terminal-non-local profile. They describe the gateway filesystem, not the configured terminal environment.
7. Keep the raw input visible and the dialog open while the RPC is pending and after a validation error. Surface the backend's profile-scoped resolution error inline, preserve the user's text for correction, prevent duplicate submit, and provide accessible labels/focus behavior for the new field.
8. Capture `{gateway, profile, generation}` when the dialog/path prompt opens and use that route for capability lookup and mutation. A live profile swap must not redirect profile A's typed path into profile B's Project DB/environment.
9. Change `addProjectFolder` and create/open flows to consume authoritative RPC responses. On success, replace optimistic/raw cache entries, active Project state, tree primary path, and any newly launched session cwd with backend-returned canonical values before background reconciliation.
10. In `openFolderAsProject`, use the canonical `cwd` returned by `projects.for_cwd`, or the created Project's canonical `primary_path`, when entering/creating a session. A non-local resolution/create failure stops the operation; the existing raw-path plain-workspace fallback is allowed only when capability is explicitly local.
11. Keep `setProjectAppearance` auto-adoption on the same canonicalizing `projects.create` route; it needs regression coverage but no additional path-entry UI.
12. Preserve stale-backend detection, captured-profile routing, reconnect-generation guards, and rollback behavior. A profile/gateway change while a dialog is open clears or closes the pending request rather than submitting the path under a different environment.
13. Add Vitest cases for all three entry modes, capability loading/failure, non-local paths never invoking native or `/api/fs` pickers, direct-path versus no-path `openFolderAsProject` entrypoints, raw `../repo` submitted unchanged, canonical `/workspace/repo` replacing it in cache/session cwd on success, remote failure never launching a raw cwd, error text/input retention, duplicate-submit prevention, and profile-switch capability invalidation.
14. Add localized labels, placeholders, explanation, validation-pending copy, and backend-error affordances to every shipped Desktop locale; keep backend names out of user-facing copy.

**Acceptance:** A Desktop user can only browse a filesystem proven to be the active profile's terminal filesystem; non-local Project paths are entered as text, validated by the mutation RPC, and replaced with the server-returned canonical path.

### Task 12: Align the Web/Kanban dashboard with backend-owned Project paths

**Objective:** Keep browser path entry explicit and move every Board/Project-directory validation off the web-server host and into the active profile's terminal environment.

**Files:**

- Modify: `plugins/kanban/dashboard/plugin_api.py`
- Modify: `plugins/kanban/dashboard/dist/index.js`
- Modify: `web/src/i18n/types.ts`
- Modify: `web/src/i18n/{en,af,ar,de,es,fr,ga,hu,it,ja,ko,pt,ru,tr,uk,zh,zh-hant}.ts`
- Test: `tests/plugins/test_kanban_board_project_api.py`
- Test: `tests/plugins/test_kanban_dashboard_plugin.py`
- Verify/no standalone Project CRUD change: `web/src/pages/ChatPage.tsx` and the Web route inventory

**Steps:**

1. Record the audited scope in implementation notes/tests: the React Web dashboard currently has no standalone Project-management page; `/chat` embeds the TUI. Do not add a speculative second Project store or browser file picker. The structured Web surface affected here is the Kanban dashboard's Board create/settings Project-directory field.
2. Keep the existing Project-directory field as text for both local and non-local server filesystems: a browser cannot safely open a system picker for the gateway or terminal backend. Update its helper copy to state that an absolute path is resolved in the Dashboard request profile's terminal environment when saved. Use trimming only to detect blank/clear; submit non-blank path text without frontend normalization.
3. Replace `plugin_api.py:_validate_workdir` controller-only handling with the shared backend-aware resolver and a `require_absolute_existing_directory` policy. Preserve the current strict local Board-workdir behavior; for non-local filesystems perform the equivalent absolute/directory/canonical checks in the terminal environment. Do not duplicate shell quoting, provider lookup, or backend classification in the plugin.
4. Do not add a Project selector or a new client-supplied `source_profile` field in this change. The existing `GET /projects` and `project_id` API capability remains untouched by the frontend; Project binding continues through existing CLI/server flows, where the server derives and snapshots source profile from the invocation context.
5. Resolve direct `default_workdir` input before Board metadata mutation using the Dashboard request profile. A Project path already inherited from a server-side binding uses the binding snapshot and is rechecked by the worker contract in Tasks 9/10. Run potentially cold environment work through the plugin's existing executor/async pattern rather than blocking the event loop.
6. Move `_default_workspace_kind()` off controller-local Git probing. Resolve Git/workspace kind when the path is created/bound in its terminal environment and persist/read the additive Board snapshot from Task 9; legacy remote/unknown rows must not call host Git as a fallback.
7. Return the canonical effective `default_workdir` and workspace kind in create/update responses. The dashboard replaces local form state and refreshed Board metadata from that response; it must not keep the raw entry after success.
8. On failure, return a stable HTTP 400 validation error without writing partial Board metadata. Keep the create/settings dialog open, preserve the entered path, render the server message beside the field, restore focus, and prevent duplicate submissions while resolution is pending.
9. In task creation, distinguish inherited Board workspace from a user override. Initializing the field from `default_workdir` must not cause the UI to send `workspace_path`; track dirty/override state and omit untouched values so backend Project binding can derive the deterministic worktree. Send `workspace_path` only after the user explicitly edits/overrides it.
10. Worker-side accessibility remains the Task 9/10 invariant and is rechecked when an assignee materializes a task; do not infer compatibility from creator, assignee, or backend-name equality.
11. Add API tests for local compatibility, plugin-remote canonicalization, missing/file/unreachable path rejection, no partial write, persisted workspace kind, and controller `Path`/Git probes being forbidden for remote paths. Add checked-in bundle contract tests for canonical/error handling and inherited-versus-overridden `workspace_path`; `dist/index.js` is the in-repo plugin source artifact, so do not invent a parallel build system.
12. Add/update Kanban translation keys for helper, pending, and validation-error text in every shipped Web locale; do not rely on English fallbacks for new permanent UI.

**Acceptance:** The Web/Kanban dashboard never validates a backend-owned directory against the web-server host, reports terminal-environment failures without losing input, and persists/displays the canonical backend path returned by the server.

### Task 13: Document user-visible semantics

**Objective:** Make the command behavior and failure boundary explicit.

**Files:**

- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/user-guide/desktop.md`
- Modify: `website/docs/user-guide/features/kanban.md`

**Steps:**

1. Document `project create` folder arguments and `project add-folder` options.
2. State that local backends keep lexical local normalization and do not require existence.
3. State that non-local terminal environments resolve and validate the folder in that environment and store the returned absolute physical path.
4. State that a non-local resolution failure rejects the command without changing the Project.
5. Document the binding contract: a Board bound to a Project uses the Project's canonical backend path, and every assignee profile must be configured to access the same durable/shared filesystem.
6. Document dispatch failure when an assignee cannot access or canonicalize the bound path; do not imply that matching backend names are required or sufficient.
7. Document Desktop's locality-aware path entry: native/gateway picker only when the profile's terminal filesystem matches it, otherwise remote path text input followed by server validation.
8. Document that the Web/Kanban dashboard's existing directory field resolves its path in the Dashboard request profile's terminal environment rather than the browser or web-server host. Do not document a new Project selector.
9. Keep wording provider-neutral; do not enumerate built-in backends.

---

## 4. Verification matrix

- **Local CLI:** relative, `~`, missing path, regular file, duplicate add, label update, and `--primary` retain current behavior.
- **Remote CLI:** relative/`~` path resolves through fake provider; missing/file path fails; canonical path is stored; failure returns 2 and leaves DB unchanged.
- **Remote RPC:** requested profile selects its own terminal policy/provider/credentials and writes only its own DB; failure returns JSON-RPC 5063.
- **Capability RPC:** filesystem locality is additive, provider-derived, profile-scoped, and conservative for unknown/older providers; querying it does not create a sandbox.
- **Agent tool:** uses its task/session environment and returns canonical `primary_path`; path-resolution failure returns `success: false` before creation/activation, and workspace-application failure is never reported as a successful move.
- **Plugin provider:** a provider name unknown to core is correctly classified through environment/provider capabilities.
- **Reference operations:** canonical stored paths can be set primary or removed; a deleted remote folder can still be removed by its exact stored path.
- **RPC scheduling:** every potentially remote Project mutation is dispatched through `_LONG_HANDLERS` and does not block the JSON-RPC reader.
- **Downstream consumption:** `projects.for_cwd` canonicalizes a remote alias in the target environment, returns that canonical cwd, and can match it back to its Project with controller-local filesystem/Git probes forbidden.
- **Desktop local connection + local filesystem:** native picker remains in use and local Project semantics are unchanged.
- **Desktop remote connection + local filesystem:** the gateway-host browser remains in use because it addresses the same filesystem as the active profile.
- **Desktop non-local/unknown filesystem:** create/add-folder and no-path open-folder entrypoints show text input and never call native or gateway-host filesystem pickers; direct-path commands submit without browsing; raw input is replaced by authoritative canonical response, while rejected input remains editable.
- **Desktop profile switch:** cached locality and pending path operations cannot cross gateway/profile generations.
- **Web/Kanban dashboard:** the existing create/settings directory input persists canonical terminal-environment results, shows failures without closing, and performs no controller `Path`/Git probe for remote paths; no new Project selector or client-controlled source-profile field is introduced.
- **Kanban task inheritance:** an untouched prefilled Board directory is omitted as `workspace_path` so Project binding derives a worktree; only a user-edited override is sent explicitly.
- **Cross-platform:** remote POSIX canonical paths are never fed into host-native `abspath`/`normcase` after resolution.
- **Bound Board inheritance:** a profile-A Project binding gives new Board tasks a self-contained canonical root/project slug without requiring the assignee profile to own the same Project row.
- **Shared backend success:** profile B reaches profile A's canonical root in its own environment, materializes the deterministic worktree there, and runs with that worktree as authoritative cwd.
- **Shared backend failure:** an assignee whose environment cannot reach the root, resolves a different canonical root, or cannot validate the Git repository is blocked before agent execution; no controller-local Git or cwd call touches the remote path.
- **Workspace precedence:** task/Board workspace wins over assignee `terminal.cwd`, while assignee backend/image/mount/network/resource policy remains effective.
- **Snapshot behavior:** rebinding affects future tasks only; existing materialized task workspaces are unchanged.

### Targeted commands

```bash
scripts/run_tests.sh \
  tests/agent/test_terminal_env_registry.py \
  tests/hermes_cli/test_project_paths.py \
  tests/hermes_cli/test_projects_db.py \
  tests/hermes_cli/test_projects_cli.py \
  tests/tui_gateway/test_projects_rpc.py \
  tests/tools/test_desktop_tools_diet.py \
  tests/agent/test_runtime_cwd.py \
  tests/hermes_cli/test_kanban_project_link.py \
  tests/plugins/test_kanban_board_project_api.py \
  tests/plugins/test_kanban_dashboard_plugin.py

node --check plugins/kanban/dashboard/dist/index.js

cd apps/desktop
npm test -- --run \
  src/store/projects.test.ts \
  src/app/chat/sidebar/project-dialog.test.tsx
npm run typecheck
npm run lint

cd ../../web
npm run check
npm run build
```

### Final gates

```bash
scripts/check_compat_pointers.py
scripts/run_tests.sh
git diff --check
```

Do not run `uv build --wheel`; this change does not require package artifacts.

---

## 5. Risks and mitigations

1. **Controller OS corrupts remote path:** require the explicit canonical-path DB contract and prohibit host path APIs after remote resolution.
2. **Shell injection/path quoting:** do not concatenate raw input into an unquoted command. Reuse the environment's cwd quoting rules or a shared quoting helper, run in a subshell, and test metacharacters.
3. **Probe changes terminal cwd:** bypass model-facing terminal finalization/session-cwd recording; assert the session cwd is unchanged after resolution.
4. **Cross-profile environment reuse:** bind full requested-profile runtime state and use a profile-qualified operation cache scope; assert A/B isolation.
5. **Plugin compatibility:** treat absent `is_local` as non-local and derive/stamp it from existing provider capability where possible; never assume an unknown plugin is host-local.
6. **Resource churn:** acquire through the existing environment cache/reaper instead of creating and immediately destroying ad hoc SSH/container environments.
7. **Removing deleted paths:** exact stored-path matching precedes backend resolution for remove/set-primary references.
8. **Partial project creation:** finish all remote probes before opening the database write transaction.
9. **Existing bad rows:** leave them untouched; automatic migration cannot know which historical backend namespace produced them.
10. **False end-to-end confidence:** explicitly test session ownership/workspace propagation and a real Project-bound worker preflight after persistence; do not stop at a correct database row.
11. **Same backend name, different filesystem:** never accept provider/backend-name equality as proof. Require the assignee environment to resolve the bound root to the exact canonical path and validate the expected Git repository.
12. **Separate environment instances:** materialize during worker bootstrap, not in a dispatcher-created probe sandbox, so validation and execution use the same assignee environment lifecycle.
13. **Profile cwd override:** task workspace remains authoritative after the child loads profile config; cover Docker auto-mount precedence explicitly.
14. **Global Board loses Project context:** persist the source profile and Project slug/path snapshot at bind time so cross-profile workers do not need to resolve a per-profile Project row.
15. **Partial reciprocal bind:** update Project and Board metadata as one reported operation with rollback/compensation; never swallow a Board write and print success.
16. **Dashboard scope creep/profile spoofing:** do not add Project selection or a client-controlled `source_profile`; direct directory validation uses the established request profile, while server-side Project binding snapshots source profile from trusted invocation context.
17. **Host Git misclassification:** persist backend-resolved workspace kind at bind/update and prohibit `_default_workspace_kind()` from probing remote paths on the controller during Board listing.
18. **Inherited path becomes an override:** track task-form dirtiness and omit untouched `workspace_path`, otherwise the backend cannot derive the Project worktree.

## 6. Expected change set

**New module:**

- `hermes_cli/project_paths.py`
- `tests/hermes_cli/test_project_paths.py`

**Core modifications:**

- `tools/terminal_tool.py`
- `tools/terminal_tool_lifecycle.py` if acquisition logic is factored there
- `tools/terminal_tool_backends.py`
- `agent/terminal_env_provider.py`
- `agent/terminal_env_registry.py`
- `hermes_cli/projects_db.py`
- `hermes_cli/projects_cmd.py`
- `tui_gateway/methods_projects.py`
- `tui_gateway/server.py`
- `tui_gateway/project_tree.py`
- `tui_gateway/agent_callbacks.py`
- `agent/runtime_cwd.py` if the end-to-end regression reaches its host-only directory guard
- `tools/project_tools.py`
- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban_db_workspace.py`
- `hermes_cli/kanban_db_dispatch.py`
- a narrow Kanban worker-bootstrap sibling selected during implementation
- `plugins/kanban/dashboard/plugin_api.py`
- `tests/plugins/test_kanban_board_project_api.py`
- `tests/plugins/test_kanban_dashboard_plugin.py`

**Client/docs modifications:**

- `apps/desktop/src/types/hermes.ts`
- `apps/desktop/src/lib/desktop-fs.ts`
- `apps/desktop/src/lib/desktop-fs.test.ts`
- `apps/desktop/src/store/projects.ts`
- `apps/desktop/src/store/projects.test.ts`
- `apps/desktop/src/app/chat/sidebar/project-dialog.tsx`
- `apps/desktop/src/app/chat/sidebar/project-dialog.test.tsx`
- `apps/desktop/src/app/chat/sidebar/index.tsx`
- optional shared Project path prompt component/test selected during implementation
- `apps/desktop/src/i18n/types.ts` and all six shipped locale files
- `plugins/kanban/dashboard/dist/index.js`
- `web/src/i18n/types.ts` and all shipped locale files
- `website/docs/reference/cli-commands.md`
- `website/docs/user-guide/desktop.md`
- `website/docs/user-guide/features/kanban.md`

No Project/frontend backend enumeration or provider-specific branch should be added. `plugins/kanban/dashboard/dist/index.js` is the checked-in plugin source artifact and may change directly; do not commit ignored `hermes_cli/web_dist`, Desktop build output, or package artifacts. Board metadata may gain additive Project-binding fields; the Kanban task schema should remain unchanged unless implementation proves that existing project/workspace/branch fields cannot carry the required provenance safely.
