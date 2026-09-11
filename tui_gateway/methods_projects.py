"""Projects RPC surface: per-profile multi-folder workspaces, repo discovery, sidebar tree.
Bodies are rebound onto server.py's globals at install (method_ctx.bind_module)."""

from __future__ import annotations

import contextlib

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


# JSON-RPC error codes: generic failure / id resolved to nothing / invalid argument.
_E_PROJECTS, _E_NO_PROJECT, _E_PROJECT_ARG = 5061, 5062, 5063


class _NoProject(Exception):
    """Raised inside a projects handler when ``params['id']`` resolves to None."""


@contextlib.contextmanager
def _project_runtime_scope():
    """Bind secrets and complete terminal policy for the already home-scoped request."""
    home = Path(get_hermes_home())
    secret_token = set_secret_scope(build_profile_secret_scope(home))
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope

    terminal_token = install_profile_terminal_scope(home)
    try:
        yield f"projects:{home}"
    finally:
        reset_terminal_scope(terminal_token)
        reset_secret_scope(secret_token)


def _project_filesystem_is_local() -> bool:
    """Current requested profile's lightweight terminal-filesystem capability."""
    with _project_runtime_scope():
        from tools.terminal_tool import _get_env_config
        from tools.terminal_tool_backends import terminal_filesystem_scope

        return terminal_filesystem_scope(_get_env_config().get("env_type", "")) == "local"


def _projects_payload(conn) -> dict:
    from hermes_cli import projects_db as pdb
    return {
        "projects": [p.to_dict() for p in pdb.list_projects(conn, include_archived=True)],
        "active_id": pdb.get_active_id(conn)}


def _projects_method(name: str):
    """Register a projects RPC, injecting (pdb, conn) and unifying error mapping; profile-scoped
    so app-global remote mode reads that profile's ``projects.db``."""
    def decorator(fn):
        @method(name)
        @_registry.profile_scoped
        def handler(rid, params: dict) -> dict:
            try:
                from hermes_cli import projects_db as pdb
                with pdb.connect_closing() as conn:
                    return fn(rid, params, pdb, conn)
            except _NoProject:
                return _err(rid, _E_NO_PROJECT, "no such project")
            except ValueError as e:
                return _err(rid, _E_PROJECT_ARG, str(e))
            except Exception as e:
                return _err(rid, _E_PROJECTS, str(e))
        return handler
    return decorator


def _require_project(pdb, conn, params: dict):
    """The project named by ``params['id']`` (or raise ``_NoProject``)."""
    proj = pdb.get_project(conn, str(params.get("id") or ""))
    if proj is None:
        raise _NoProject
    return proj


def _pick(params: dict, *keys: str) -> dict:
    return {k: params.get(k) for k in keys}


def _register_project_mutator(suffix: str, fn_name: str, takes_path: bool, kwargs_of) -> None:
    """``projects.<suffix>``: resolve ``params['id']`` (5062 when missing), call
    ``pdb.<fn_name>(conn, id[, path], **kwargs_of(params))``, answer with the refreshed project."""
    @_projects_method(f"projects.{suffix}")
    def _(rid, params, pdb, conn) -> dict:
        proj = _require_project(pdb, conn, params)
        args = ()
        kwargs = kwargs_of(params)
        if takes_path:
            from hermes_cli.project_paths import (
                resolve_project_folder, resolve_project_folder_reference,
            )
            with _project_runtime_scope() as operation_scope:
                raw = str(params.get("path") or "")
                canonical = (
                    resolve_project_folder(raw, operation_scope=operation_scope)
                    if fn_name == "add_folder" else
                    resolve_project_folder_reference(
                        proj, raw, operation_scope=operation_scope))
            args = (canonical,)
            kwargs["canonical_paths"] = True
        getattr(pdb, fn_name)(conn, proj.id, *args, **kwargs)
        return _ok(rid, {"project": pdb.get_project(conn, proj.id).to_dict()})


_register_project_mutator(
    "update", "update_project", False,
    lambda p: _pick(p, "name", "description", "icon", "color", "board_slug"))
_register_project_mutator(
    "add_folder", "add_folder", True,
    lambda p: {"label": p.get("label"), "is_primary": bool(p.get("is_primary"))})
_register_project_mutator("remove_folder", "remove_folder", True, lambda p: {})
_register_project_mutator("set_primary", "set_primary", True, lambda p: {})


@_projects_method("projects.list")
def _(rid, params, pdb, conn) -> dict:
    return _ok(rid, _projects_payload(conn))


@_projects_method("projects.get")
def _(rid, params, pdb, conn) -> dict:
    return _ok(rid, {"project": _require_project(pdb, conn, params).to_dict()})


@_projects_method("projects.create")
def _(rid, params, pdb, conn) -> dict:
    from hermes_cli.project_paths import resolve_project_folders

    with _project_runtime_scope() as operation_scope:
        folders, primary = resolve_project_folders(
            params.get("folders") or [], params.get("primary_path"),
            operation_scope=operation_scope)
    pid = pdb.create_project(
        conn, name=str(params.get("name") or ""), folders=folders, primary_path=primary,
        canonical_paths=True,
        **_pick(params, "slug", "description", "icon", "color", "board_slug"))
    if params.get("use"):
        pdb.set_active(conn, pid)
    proj = pdb.get_project(conn, pid)
    return _ok(rid, {"project": proj.to_dict() if proj else None})


@method("projects.capabilities")
@_registry.profile_scoped
def _(rid, params: dict) -> dict:
    try:
        with _project_runtime_scope():
            from tools.terminal_tool import _get_env_config
            from tools.terminal_tool_backends import terminal_filesystem_scope

            scope = terminal_filesystem_scope(_get_env_config().get("env_type", ""))
        return _ok(rid, {"filesystem_scope": scope})
    except Exception:
        return _ok(rid, {"filesystem_scope": "unknown"})


@_projects_method("projects.archive")
def _(rid, params, pdb, conn) -> dict:
    proj = _require_project(pdb, conn, params)
    (pdb.restore_project if params.get("restore") else pdb.archive_project)(conn, proj.id)
    return _ok(rid, _projects_payload(conn))


@_projects_method("projects.delete")
def _(rid, params, pdb, conn) -> dict:
    pdb.delete_project(conn, _require_project(pdb, conn, params).id)
    return _ok(rid, _projects_payload(conn))


@_projects_method("projects.set_active")
def _(rid, params, pdb, conn) -> dict:
    pdb.set_active(conn, _require_project(pdb, conn, params).id if params.get("id") else None)
    return _ok(rid, {"active_id": pdb.get_active_id(conn)})


@_projects_method("projects.for_cwd")
def _(rid, params, pdb, conn) -> dict:
    from hermes_cli.project_paths import resolve_project_folder
    from tools.terminal_tool import acquire_terminal_environment

    with _project_runtime_scope() as operation_scope:
        raw = str(params.get("cwd") or "").strip()
        try:
            env = acquire_terminal_environment(operation_scope=operation_scope)
        except Exception:
            raise ValueError(
                f"could not resolve project folder {raw!r} in the terminal environment") from None
        if not raw:
            raw = _completion_cwd({}) if getattr(env, "is_local", False) is True else "."
        cwd = resolve_project_folder(
            raw, operation_scope=operation_scope, environment=env)
    proj = pdb.project_for_path(conn, cwd)
    branch = git_probe.branch(cwd) if getattr(env, "is_local", False) is True else ""
    return _ok(rid, {
        "project": proj.to_dict() if proj else None, "cwd": cwd,
        "branch": branch})


def _non_workspace_dirs() -> set[str]:
    """Never-a-workspace dirs: ``/``, the user's home, the dir homes live in, plus both POSIX
    spellings on every host (remote shells hand back Linux paths; promoting one mints a
    catch-all project)."""
    home = os.path.realpath(os.path.expanduser("~"))
    candidates = (os.sep, home, os.path.dirname(home), "/home", "/Users")
    return {os.path.normcase(os.path.realpath(path)) for path in candidates if path}


def _is_repo_junk(root: str) -> bool:
    """A git root never auto-surfaced as a project: a non-workspace dir or anything under
    HERMES_HOME. User-created projects pointing there are still honored."""
    if not root:
        return True
    from hermes_constants import get_hermes_home
    real = os.path.realpath(root)
    hermes_home = os.path.realpath(str(get_hermes_home()))
    return (
        os.path.normcase(real) in _non_workspace_dirs()
        or real == hermes_home
        or real.startswith(hermes_home + os.sep))


def _is_session_cwd_junk(cwd: str) -> bool:
    """A non-git cwd that stays in flat Recents. A DESCENDANT of HERMES_HOME may be an
    intentional prose/data workspace, so only HERMES_HOME itself is excluded here."""
    if not cwd:
        return True
    from hermes_constants import get_hermes_home
    real = os.path.normcase(os.path.realpath(cwd))
    hermes_home = os.path.normcase(os.path.realpath(str(get_hermes_home())))
    return real in _non_workspace_dirs() or real == hermes_home


def _is_backend_cwd_junk(cwd: str) -> bool:
    """Conservative junk detection without interpreting a backend path on this host."""
    from hermes_cli.project_paths import canonical_path_key, path_segments

    if not cwd:
        return True
    style, key = canonical_path_key(cwd)
    if style == "posix":
        return key in {".", "/", "/home", "/Users"}
    segments = path_segments(cwd)
    return len(segments) == 1 and segments[0].endswith(":")


def _repo_discovery_policy(raw: dict | None = None) -> dict:
    """Return the effective, profile-local Desktop repository scan policy."""
    from hermes_cli.config import DEFAULT_CONFIG
    defaults = DEFAULT_CONFIG["desktop"]
    source = raw if isinstance(raw, dict) else (_load_cfg().get("desktop") or {})
    if not isinstance(source, dict):
        source = {}

    def _get(short: str, long: str):
        return source.get(short, source.get(long, defaults[long]))

    def _paths(short: str, long: str) -> list[str]:
        values = _get(short, long)
        if not isinstance(values, list):
            return list(defaults[long])
        return [v.strip() for v in values if isinstance(v, str) and v.strip()]
    enabled = _get("enabled", "repo_scan_enabled")
    return {
        "enabled": enabled if isinstance(enabled, bool) else defaults["repo_scan_enabled"],
        "roots": _paths("roots", "repo_scan_roots"),
        "exclude_paths": _paths("exclude_paths", "repo_scan_exclude_paths")}


def _repo_discovery_policy_key(policy: dict) -> str:
    def _paths(values: list[str]) -> list[str]:
        home = os.path.expanduser("~")
        return sorted({
            os.path.normcase(os.path.abspath(os.path.join(home, os.path.expanduser(v))))
            for v in values})
    canonical = {
        "enabled": bool(policy["enabled"]), "roots": _paths(policy["roots"]),
        "exclude_paths": _paths(policy["exclude_paths"])}
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def _repo_discovery_policy_is_default(policy: dict) -> bool:
    from hermes_cli.config import DEFAULT_CONFIG
    return _repo_discovery_policy_key(policy) == _repo_discovery_policy_key(
        _repo_discovery_policy(DEFAULT_CONFIG["desktop"]))


def _scan_discovered_repos_remote(conn, policy: dict) -> bool:
    """Backend-side disk scan of the policy roots into the discovery cache. Best-effort:
    failures log and leave the cache untouched. True only when the scan is authoritative
    (every root walked to completion, cap not hit) — only then is the cache write
    ``replace=True``; a partial/errored scan must MERGE, or a failed refresh blanks the sidebar.

    The desktop's native repo scan only runs on the local filesystem. On a remote gateway connection the
    host must scan its own disk so repos with zero Hermes sessions still appear in the sidebar (#81723).
    Mirrors the desktop's behavior: walk each root (bounded depth), find `.git` directories, record (root,
    label) pairs into the discovery cache.
    See #81723.
    """
    from hermes_cli import projects_db as pdb
    roots = policy.get("roots") or []
    excludes = policy.get("exclude_paths") or []
    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    authoritative = True

    def _is_excluded(path: str) -> bool:
        return any(
            path == ex or path.startswith(ex.rstrip("/\\") + os.sep) for ex in excludes if ex)
    for root in roots:
        if not os.path.isdir(root):
            # `os.walk` on a missing root yields nothing; an unmounted volume must not wipe.
            authoritative = False
            logger.debug("discover_repos scan root missing, skipping: %s", root)
            continue
        try:
            for dirpath, dirnames, _filenames in os.walk(root):
                if _is_excluded(dirpath):
                    dirnames[:] = []
                elif ".git" in dirnames:  # check BEFORE pruning hidden dirs — `.git` is hidden
                    if dirpath not in seen:
                        seen.add(dirpath)
                        pairs.append((dirpath, os.path.basename(dirpath)))
                    dirnames[:] = []  # don't hunt nested repos inside a repo
                else:
                    dirnames[:] = [
                        d for d in dirnames if not d.startswith(".") and d != "node_modules"]
                if len(pairs) >= 500:
                    break
        except Exception:
            authoritative = False
            logger.debug("discover_repos scan failed for root %s", root, exc_info=True)
        if len(pairs) >= 500:  # cap hit: the walk didn't cover the full roots
            authoritative = False
            break
    if pairs:
        try:
            pdb.record_discovered_repos(
                conn, pairs, replace=authoritative, policy_key=_repo_discovery_policy_key(policy))
        except Exception:
            logger.debug("discover_repos cache write failed", exc_info=True)
            authoritative = False
    return authoritative


def _discover_repos_payload(
    db, *, conn=None, backfill: bool = True, include_cached: bool = True) -> list[dict]:
    """Merge cached filesystem-scanned repos with session-derived roots, junk-filtered, with
    session totals. ``backfill`` persists resolved roots onto session rows — kept OFF the
    per-turn tree path and done only on explicit refresh."""
    repos: dict[str, dict] = {}

    def _agg(root: str) -> dict:
        return repos.setdefault(
            root, {"root": root, "label": "", "sessions": 0, "last_active": 0.0})
    cwd_rows = list(db.distinct_session_cwds())
    # Parallel-warm the per-cwd git probes so a cold first paint doesn't serialize them.
    git_probe.warm_roots(str(r.get("cwd") or "") for r in cwd_rows)
    cwd_to_root: dict[str, str] = {}
    for row in cwd_rows:
        cwd = str(row.get("cwd") or "")
        root = git_probe.common_repo_root(cwd)
        if not root:
            continue
        cwd_to_root[cwd] = root
        if _is_repo_junk(root):
            continue
        agg = _agg(root)
        agg["sessions"] += int(row.get("sessions") or 0)
        agg["last_active"] = max(agg["last_active"], float(row.get("last_active") or 0))
    if backfill:
        try:
            db.backfill_repo_roots(cwd_to_root)
        except Exception:
            logger.debug("failed to backfill repo roots", exc_info=True)
    if include_cached:
        # `last_seen` is scan time, not user activity — never fold it into `last_active`.
        try:
            from hermes_cli import projects_db as pdb
            with (contextlib.nullcontext(conn) if conn is not None else pdb.connect_closing()) as c:
                for entry in pdb.list_discovered_repos(c):
                    root = str(entry.get("root") or "")
                    if root and not _is_repo_junk(root):
                        agg = _agg(root)
                        if entry.get("label"):
                            agg["label"] = entry["label"]
        except Exception:
            logger.debug("failed to read discovered repo cache", exc_info=True)
    out = sorted(repos.values(), key=lambda r: r["last_active"], reverse=True)
    for r in out:
        r["label"] = r["label"] or os.path.basename(r["root"].rstrip("/\\")) or r["root"]
    return out


# Not user conversations; subagent/compression children are dropped by include_children=False.
_PROJECT_TREE_EXCLUDED_SOURCES = ["cron", "kanban"]


def _project_tree_row(r: dict) -> dict:
    """Project a SessionDB row to the minimal shape the sidebar renders (grouping fields +
    what ``SidebarSessionRow`` reads), minus the heavy columns."""
    row = {k: r.get(k) for k in (
        "id", "_lineage_root_id", "_lineage_ids", "parent_session_id", "title", "preview")}
    row.update(
        started_at=r.get("started_at") or 0, ended_at=r.get("ended_at"),
        last_active=r.get("last_active") or r.get("started_at") or 0,
        source=r.get("source"), archived=bool(r.get("archived")),
        **{k: r.get(k) or 0 for k in (
            "message_count", "tool_call_count", "input_tokens", "output_tokens")},
        **{k: r.get(k) for k in ("actual_cost_usd", "estimated_cost_usd", "model")},
        is_active=False, **{k: r.get(k) for k in ("cwd", "git_branch", "git_repo_root")})
    return row


def _project_tree_inputs(
    db, session_limit: int, *, include_discovered: bool, filesystem_local: bool = True,
) -> tuple[list[dict], list[dict], list[dict], str | None]:
    """Gather (sessions, projects, discovered_repos, active_id) for build_tree.
    ``include_discovered`` is the zero-session-repo overview tier; drill-in skips it (and
    the distinct-cwd scan + git probes) on that per-turn path."""
    rows = db.list_sessions_rich(
        limit=session_limit, offset=0, order_by_last_active=True, min_message_count=1,
        include_children=False, exclude_sources=_PROJECT_TREE_EXCLUDED_SOURCES,
        include_archived=False, compact_rows=True)
    sessions = [_project_tree_row(r) for r in rows]
    if filesystem_local:
        git_probe.warm_roots(s["cwd"] for s in sessions if s.get("cwd"))
    from hermes_cli import projects_db as pdb
    policy = _repo_discovery_policy()
    policy_key = _repo_discovery_policy_key(policy)
    with pdb.connect_closing() as conn:
        if include_discovered and filesystem_local:
            pdb.reconcile_discovered_repos_policy(
                conn, policy_key, preserve_unversioned=_repo_discovery_policy_is_default(policy))
        projects = [p.to_dict() for p in pdb.list_projects(conn)]
        active_id = pdb.get_active_id(conn)
        discovered = []
        if include_discovered and filesystem_local:
            discovered = _discover_repos_payload(
                db, conn=conn, backfill=False, include_cached=policy["enabled"])
    return sessions, projects, discovered, active_id


# Per-build memo for `_dir_exists_cached`; cleared by every `_build_project_tree`.
_DIR_EXISTS_CACHE: dict[str, bool] = {}


def _dir_exists_cached(path: str) -> bool:
    """``os.path.isdir`` memoized per build — ``build_tree`` asks per SESSION, not per path."""
    hit = _DIR_EXISTS_CACHE.get(path)
    if hit is None:
        hit = _DIR_EXISTS_CACHE[path] = os.path.isdir(path)
    return hit


def _build_project_tree(
    db, *, preview_limit: int, hydrate: bool, session_limit: int, include_discovered: bool
) -> tuple[dict, str | None]:
    """Gather inputs and run the one authoritative builder. Returns (tree, active_id)."""
    from tui_gateway import project_tree
    _DIR_EXISTS_CACHE.clear()
    filesystem_local = _project_filesystem_is_local()
    sessions, projects, discovered, active_id = _project_tree_inputs(
        db, session_limit, include_discovered=include_discovered,
        filesystem_local=filesystem_local)
    if filesystem_local:
        git_probe.warm_roots(
            [str(f.get("path") or "") for p in projects for f in (p.get("folders") or [])]
            + [str(r.get("root") or "") for r in discovered])
    junk_root = _is_repo_junk if filesystem_local else _is_backend_cwd_junk
    junk_cwd = _is_session_cwd_junk if filesystem_local else _is_backend_cwd_junk
    tree = project_tree.build_tree(
        projects, sessions, discovered,
        git_probe.resolve if filesystem_local else (lambda _path: None), preview_limit=preview_limit,
        hydrate=hydrate, is_junk_root=junk_root, is_junk_cwd=junk_cwd,
        exists=_dir_exists_cached if filesystem_local else (lambda _path: True))
    return tree, active_id


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
