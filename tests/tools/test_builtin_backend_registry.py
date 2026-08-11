from __future__ import annotations

from dataclasses import fields, replace
from threading import Barrier, Thread

import pytest

from tools.environments import BackendCapabilities, ExecutionLocation, FilesystemSemantics
from tools.environments.builtin_backends import (
    RESERVED_BUILTIN_BACKEND_NAMES,
    SELECTABLE_BUILTIN_BACKEND_NAMES,
    _builtin_backend_definitions,
)
from tools.environments.manager import EnvironmentManager
from tools.environments.registry import (
    BackendAlreadyRegisteredError,
    TerminalBackendRegistry,
)


_SELECTABLE_BUILTINS = {
    "local",
    "docker",
    "singularity",
    "modal",
    "daytona",
    "vercel_sandbox",
    "ssh",
}


@pytest.fixture
def registry() -> TerminalBackendRegistry:
    registry = TerminalBackendRegistry()
    EnvironmentManager(registry=registry)
    return registry


def test_manager_registers_every_selectable_legacy_builtin(registry):
    assert {definition.name for definition in registry.list_definitions()} == (
        _SELECTABLE_BUILTINS
    )
    assert "managed_modal" in RESERVED_BUILTIN_BACKEND_NAMES
    assert registry.get("managed_modal") is None


def test_selectable_and_reserved_names_are_derived_from_canonical_inventory():
    canonical_names = {
        definition.name for definition in _builtin_backend_definitions()
    }

    assert canonical_names == _SELECTABLE_BUILTINS
    assert SELECTABLE_BUILTIN_BACKEND_NAMES == canonical_names
    assert RESERVED_BUILTIN_BACKEND_NAMES == canonical_names | {"managed_modal"}


@pytest.mark.parametrize(
    ("name", "location", "filesystem", "default_cwd", "accepts_host_cwd"),
    [
        ("local", ExecutionLocation.LOCAL, FilesystemSemantics.HOST, "", True),
        ("docker", ExecutionLocation.REMOTE, FilesystemSemantics.ISOLATED, "/root", True),
        ("singularity", ExecutionLocation.REMOTE, FilesystemSemantics.ISOLATED, "/root", False),
        ("modal", ExecutionLocation.REMOTE, FilesystemSemantics.ISOLATED, "/root", False),
        ("daytona", ExecutionLocation.REMOTE, FilesystemSemantics.ISOLATED, "/root", False),
        (
            "vercel_sandbox",
            ExecutionLocation.REMOTE,
            FilesystemSemantics.ISOLATED,
            "/vercel/sandbox",
            False,
        ),
        ("ssh", ExecutionLocation.REMOTE, FilesystemSemantics.ISOLATED, "~", False),
    ],
)
def test_builtin_definition_declares_legacy_execution_semantics(
    registry, name, location, filesystem, default_cwd, accepts_host_cwd
):
    definition = registry.require(name)

    assert definition.source == "builtin"
    assert definition.plugin_name == ""
    assert definition.capabilities.execution_location is location
    assert definition.capabilities.filesystem_semantics is filesystem
    assert definition.capabilities.accepts_host_cwd is accepts_host_cwd
    assert definition.capabilities.requires_sandbox_cwd is (
        filesystem is FilesystemSemantics.ISOLATED
    )
    assert definition.capabilities.supports_background_processes is True
    assert definition.default_cwd == default_cwd


@pytest.mark.parametrize("name", sorted(_SELECTABLE_BUILTINS))
def test_builtin_registration_is_idempotent(registry, name):
    from tools.environments.builtin_backends import register_builtin_terminal_backends

    original = registry.require(name)
    register_builtin_terminal_backends(registry)

    assert registry.require(name) == original


def test_concurrent_builtin_registration_is_atomic(monkeypatch):
    from tools.environments.builtin_backends import register_builtin_terminal_backends

    registry = TerminalBackendRegistry()
    original_get = registry.get
    first_lookup = Barrier(2)

    def synchronized_get(name):
        definition = original_get(name)
        if name == "local":
            first_lookup.wait(timeout=5)
        return definition

    monkeypatch.setattr(registry, "get", synchronized_get)
    errors = []

    def bootstrap():
        try:
            register_builtin_terminal_backends(registry)
        except Exception as exc:  # pragma: no branch - assertion captures the failure
            errors.append(exc)

    threads = [Thread(target=bootstrap), Thread(target=bootstrap)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert {definition.name for definition in registry.list_definitions()} == (
        _SELECTABLE_BUILTINS
    )


@pytest.mark.parametrize("canonical", _builtin_backend_definitions(), ids=lambda d: d.name)
@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("factory", lambda _request: object()),
        ("label", "Counterfeit label"),
        ("description", "Counterfeit description"),
        ("availability_check", lambda: False),
        ("config_schema", {"counterfeit": {"type": "string"}}),
        ("install_hint", "counterfeit install hint"),
        ("diagnostic_metadata", {"counterfeit": True}),
        ("source", "plugin"),
        ("plugin_name", "counterfeit-plugin"),
        ("default_cwd", "/counterfeit"),
        ("config_resolver", lambda: {"counterfeit": True}),
    ],
)
def test_manager_rejects_every_altered_builtin_definition_field(
    canonical, field_name, replacement
):
    registry = TerminalBackendRegistry()
    registry.register(replace(canonical, **{field_name: replacement}))

    with pytest.raises(BackendAlreadyRegisteredError, match=canonical.name):
        EnvironmentManager(registry=registry)


@pytest.mark.parametrize("canonical", _builtin_backend_definitions(), ids=lambda d: d.name)
@pytest.mark.parametrize(
    "capability_field",
    [field.name for field in fields(BackendCapabilities)],
)
def test_manager_rejects_every_altered_builtin_capability(
    canonical, capability_field
):
    current = getattr(canonical.capabilities, capability_field)
    if isinstance(current, bool):
        replacement = not current
    elif capability_field == "execution_location":
        replacement = (
            ExecutionLocation.LOCAL
            if current is not ExecutionLocation.LOCAL
            else ExecutionLocation.REMOTE
        )
    else:
        replacement = (
            FilesystemSemantics.HOST
            if current is not FilesystemSemantics.HOST
            else FilesystemSemantics.ISOLATED
        )

    altered = replace(
        canonical,
        capabilities=replace(
            canonical.capabilities,
            **{capability_field: replacement},
        ),
    )
    registry = TerminalBackendRegistry()
    registry.register(altered)

    with pytest.raises(BackendAlreadyRegisteredError, match=canonical.name):
        EnvironmentManager(registry=registry)
