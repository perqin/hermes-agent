"""
Terminal Environment Provider ABC
=================================

Defines the pluggable-backend interface for terminal execution environments
(cloud sandboxes, remote runners). Providers register instances via
:meth:`PluginContext.register_terminal_environment_provider`; the dispatch
ladder in :func:`tools.terminal_tool._create_environment` consults the
registry for any ``TERMINAL_ENV`` / ``terminal.backend`` value that is not a
built-in backend (local, docker, singularity, modal, daytona,
vercel_sandbox, ssh).

Providers live in ``~/.hermes/plugins/<name>/`` (user, opt-in via
``plugins.enabled``) or ship as standalone plugin repos. Built-in backends
stay in-tree under ``tools/environments/`` — this extension point exists so
third-party sandbox vendors do NOT have to live in core (see AGENTS.md:
"Third-party products ... ship them as a standalone plugin repo").

This ABC mirrors :class:`agent.browser_provider.BrowserProvider` — same
registration flow, same scope semantics, same plugin-context gating.

Classification contract
-----------------------

Beyond creating environments, a terminal backend participates in several
core policy decisions that were historically frozensets of built-in names.
Each is expressed as a declarative attribute so the class of "new backend
missed classification site N" bugs (see PR #30112's seven-site sweep) cannot
recur for plugin backends:

* ``is_remote`` — commands run somewhere other than the host machine.
  Suppresses host OS/home/cwd hints in the system prompt, the host Python
  env probe, and remote-aware skill env handling.
* ``is_container`` — the backend behaves like a container/sandbox with its
  own filesystem rooted away from the host: container resource config is
  passed through, host-looking cwds are sanitized, and file tools use
  container path resolution.
* ``skip_container_guards`` — the sandbox is isolated enough that
  dangerous-command approval prompts are skipped (a wiped filesystem is
  disposable). Defaults to ``is_container``. Backends that can mount host
  paths should override to ``False``.
* ``cache_path_base`` — where auto-synced ``~/.hermes/cache`` files land
  inside the backend (e.g. ``"~/.hermes"`` for home-synced backends,
  ``"/root/.hermes"`` for root-homed containers), or ``None`` when host
  paths remain correct (nothing is translated).
* ``strip_env_keys`` — credential env var names owned by this backend
  (API tokens for the sandbox vendor). Stripped from every subprocess the
  agent spawns so a model-authored command can never read them.
* ``session_isolated_when_nonpersistent`` — non-persistent mode gives each
  session its own sandbox identity instead of sharing one (the #82731
  contract; opt in when a shared name would let two ephemeral runs attach
  and destroy each other's sandbox).

Environment object contract
---------------------------

:meth:`create_environment` returns an object satisfying the same duck-typed
interface as :class:`tools.environments.base.BaseEnvironment` (``execute()``,
``cleanup()`` …). Subclassing ``BaseEnvironment`` is recommended but not
required — the registry does not isinstance-check the returned environment.
The factory stamps ``_hermes_backend_name`` on the returned object so
file-path resolution can identify plugin backends without class-name
sniffing.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from copy import deepcopy
import json
import re
from typing import Any, Dict, List, Optional, Tuple


_CONFIG_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_UNSAFE_CONFIG_PATH_SEGMENTS = frozenset({"__proto__", "constructor", "prototype"})
_DASHBOARD_CONFIG_FIELD_TYPES = frozenset(
    {"boolean", "list", "number", "secret", "select", "string", "text"}
)
_SECRET_VALUE_METADATA_KEYS = frozenset(
    {"const", "default", "example", "examples", "options", "value"}
)


class TerminalEnvironmentProvider(abc.ABC):
    """Abstract base class for a pluggable terminal execution backend."""

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used as the ``terminal.backend`` /
        ``TERMINAL_ENV`` value.

        Lowercase, ``[a-z0-9_]``. Must not collide with a built-in backend
        name (local, docker, singularity, modal, managed_modal, daytona,
        vercel_sandbox, ssh) — the registry rejects such registrations.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label for pickers. Defaults to ``name``."""
        return self.name

    @property
    def description(self) -> str:
        """One-line description shown in backend pickers."""
        return f"Run commands in a {self.display_name} environment."

    # ------------------------------------------------------------------
    # Classification flags (see module docstring)
    # ------------------------------------------------------------------

    is_remote: bool = True
    is_container: bool = True
    session_isolated_when_nonpersistent: bool = False

    @property
    def skip_container_guards(self) -> bool:
        """Whether dangerous-command approval prompts are skipped."""
        return self.is_container

    @property
    def cache_path_base(self) -> Optional[str]:
        """Base dir for synced Hermes cache files inside the backend."""
        return None

    @property
    def strip_env_keys(self) -> frozenset:
        """Backend-owned credential env var names to strip from subprocesses."""
        return frozenset()

    @property
    def env_description(self) -> str:
        """Prompt-builder fallback description of where commands run.

        Used when the live backend probe fails at system-prompt build time,
        e.g. ``"a Daytona workspace (Linux)"``.
        """
        return f"a {self.display_name} environment (likely Linux)"

    # ------------------------------------------------------------------
    # Availability / setup UX
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return True when this backend can service commands.

        Cheap check only (env var present, SDK importable). Must NOT make
        network calls — this runs during requirement checks and UI paints.
        """

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        """Full requirements check for :func:`check_terminal_requirements`.

        ``config`` is the merged terminal env config dict. When provider-owned
        config is available, its resolved snapshot is included at
        ``config["backend_config"]``. Default defers to :meth:`is_available`.
        Log actionable errors before returning False.
        """
        return self.is_available()

    def probe(self) -> Tuple[str, str]:
        """Dashboard picker health probe: ``(status, detail)``.

        ``status`` is ``"ready"`` / ``"needs_setup"`` / ``"unavailable"``;
        ``detail`` carries setup guidance for non-ready rows. Must never
        raise and must stay fast (<~2s).
        """
        if self.is_available():
            return ("ready", "")
        return ("needs_setup", f"{self.display_name} is not configured.")

    def probe_with_config(self, config: Mapping[str, Any]) -> Tuple[str, str]:
        """Config-aware picker probe; defaults to the legacy no-argument probe."""
        return self.probe()

    def setup_instructions(self) -> List[str]:
        """Lines printed by ``hermes setup`` after this backend is selected.

        Use for token acquisition hints, SDK install commands, etc. The
        wizard persists ``terminal.backend`` itself; providers that need an
        interactive flow can run it in :meth:`post_setup`.
        """
        return []

    def post_setup(self) -> None:
        """Optional interactive setup hook run by ``hermes setup`` after the
        backend is selected (prompt for tokens, install SDKs). Default no-op.
        """

    def doctor_checks(self) -> List[Tuple[bool, str, str]]:
        """``hermes doctor`` rows: ``(ok, label, detail)`` triples.

        Default: a single row reflecting :meth:`is_available`.
        """
        ok = False
        try:
            ok = bool(self.is_available())
        except Exception:
            ok = False
        detail = "(configured)" if ok else "(not configured — see setup instructions)"
        return [(ok, f"{self.display_name} backend", detail)]

    # ------------------------------------------------------------------
    # Provider-owned configuration
    # ------------------------------------------------------------------

    def get_config_schema(self) -> Dict[str, Dict[str, Any]]:
        """Dashboard schema for config stored below ``terminal.backends.<name>``.

        Keys are backend-local dotted paths. Supported field types are
        ``string``, ``text``, ``secret``, ``number``, ``boolean``, ``list``,
        and ``select``. The default is no provider-specific configuration.
        """
        return {}

    def validated_config_schema(self) -> Dict[str, Dict[str, Any]]:
        """Return a validated, JSON-safe snapshot of ``get_config_schema``."""
        raw_schema = self.get_config_schema()
        if not isinstance(raw_schema, Mapping):
            raise TypeError("terminal provider config schema must be a mapping")

        schema: Dict[str, Dict[str, Any]] = {}
        for key, raw_entry in raw_schema.items():
            if not isinstance(key, str) or not key:
                raise TypeError("terminal provider config schema keys must be non-empty strings")
            segments = key.split(".")
            if any(
                not _CONFIG_PATH_SEGMENT_RE.fullmatch(segment)
                or segment in _UNSAFE_CONFIG_PATH_SEGMENTS
                for segment in segments
            ):
                raise ValueError(f"terminal provider config schema key {key!r} is unsafe")
            if not isinstance(raw_entry, Mapping):
                raise TypeError(f"terminal provider config schema entry {key!r} must be a mapping")

            entry = deepcopy(dict(raw_entry))
            if any(not isinstance(metadata_key, str) for metadata_key in entry):
                raise TypeError(
                    f"terminal provider config schema entry {key!r} metadata keys must be strings"
                )
            field_type = entry.get("type", "string")
            if field_type not in _DASHBOARD_CONFIG_FIELD_TYPES:
                raise ValueError(
                    f"terminal provider config schema entry {key!r} has unsupported type {field_type!r}"
                )
            entry["type"] = field_type
            if field_type == "secret":
                forbidden = sorted(_SECRET_VALUE_METADATA_KEYS.intersection(entry))
                if forbidden:
                    raise ValueError(
                        f"secret config schema entry {key!r} cannot include value-bearing metadata: "
                        + ", ".join(forbidden)
                    )
            options = entry.get("options")
            if field_type == "select" and (
                not isinstance(options, list)
                or any(not isinstance(option, str) for option in options)
            ):
                raise TypeError(
                    f"terminal provider config schema entry {key!r} options must be a list of strings"
                )
            try:
                json.dumps(entry, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"terminal provider config schema entry {key!r} must be JSON-serializable"
                ) from exc
            schema[key] = entry
        return schema

    def resolve_config(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        """Resolve a defensive backend-local profile config snapshot.

        Providers may override this to apply defaults and environment/secret
        fallbacks. Core passes the returned mapping to ``create_environment``
        as ``backend_config``.
        """
        return deepcopy(dict(config))

    def validated_config(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        """Resolve and defensively snapshot backend-local runtime config."""
        raw_snapshot = deepcopy(dict(config))
        resolved = self.resolve_config(deepcopy(raw_snapshot))
        if not isinstance(resolved, Mapping):
            raise TypeError(
                f"Terminal provider {self.name!r} resolve_config() must return a mapping"
            )
        return deepcopy(dict(resolved))

    # ------------------------------------------------------------------
    # The factory
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: Optional[str] = None,
        container_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """Create and return an execution environment instance.

        MUST accept ``**kwargs`` and ignore unknown keys — the forward-compat
        contract that lets the factory signature evolve without breaking
        older plugins.

        Args:
            cwd: Working directory inside the backend.
            timeout: Default per-command timeout in seconds.
            task_id: Task identifier for environment reuse/persistence keying.
            image: Configured container image name (may be irrelevant).
            container_config: Resource config dict (``container_cpu``,
                ``container_memory``, ``container_disk``,
                ``container_persistent``) when :attr:`is_container` is True.
            backend_config: Resolved provider-owned config supplied through
                ``kwargs``. Providers that consume it should declare the
                optional keyword explicitly while retaining ``**kwargs``.

        Returns:
            An object satisfying the ``BaseEnvironment`` duck-typed contract.
        """
