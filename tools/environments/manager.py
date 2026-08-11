"""Host-owned resolver and factory for terminal backend environments."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tools.environments.definitions import (
    BackendDefinition,
    BackendFactoryRequest,
    ExecutionLocation,
)
from tools.environments.registry import (
    BackendUnavailableError,
    TerminalBackendRegistry,
    terminal_backend_registry,
)

if TYPE_CHECKING:
    from tools.environments.base import BaseEnvironment


class EnvironmentManager:
    """Resolve registered backends and create their environments."""

    def __init__(self, registry: TerminalBackendRegistry | None = None) -> None:
        self.registry = registry if registry is not None else terminal_backend_registry

        from tools.environments.builtin_backends import (
            register_builtin_terminal_backends,
        )

        register_builtin_terminal_backends(self.registry)

    def resolve_backend(self, name: str) -> BackendDefinition:
        """Resolve the selected backend definition."""
        definition = self.registry.require(name)
        if not definition.is_available():
            raise BackendUnavailableError(f"Terminal backend {name!r} is unavailable")
        return definition

    def create_environment(self, request: BackendFactoryRequest) -> "BaseEnvironment":
        """Create a new environment for a fully resolved request."""
        from tools.environments.base import BaseEnvironment
        from tools.environments.builtin_backends import (
            is_canonical_builtin_definition,
        )

        definition = self.registry.require(request.backend_name)
        # Canonical built-ins construct directly and surface the backend-specific
        # constructor/configuration error. Their separate
        # check_terminal_requirements() path still performs the preflight.
        # Third-party definitions retain manager-enforced availability.
        if (
            not is_canonical_builtin_definition(definition)
            and not definition.is_available()
        ):
            raise BackendUnavailableError(
                f"Terminal backend {request.backend_name!r} is unavailable"
            )
        resolved_config: dict[str, Any] = {}
        if definition.config_resolver is not None:
            plugin_config = definition.config_resolver()
            if not isinstance(plugin_config, Mapping):
                raise TypeError(
                    f"Terminal backend {definition.name!r} config_resolver must return a mapping"
                )
            resolved_config.update(plugin_config)
        resolved_config.update(request.backend_config)
        request.backend_config = resolved_config

        environment = definition.factory(request)
        if not isinstance(environment, BaseEnvironment):
            raise TypeError(
                f"Terminal backend {definition.name!r} factory must return BaseEnvironment"
            )
        if definition.capabilities.execution_location is not ExecutionLocation.LOCAL:
            from tools.environments.local import LocalEnvironment

            if isinstance(environment, LocalEnvironment):
                raise TypeError(
                    f"Terminal backend {definition.name!r} declares "
                    f"{definition.capabilities.execution_location.value} execution "
                    "but returned LocalEnvironment"
                )
        return environment
