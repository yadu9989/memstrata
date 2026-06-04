"""
Phase 15' — cd-hook generation tests.

Verifies that each shell produces a structurally correct hook block with
the required idempotency markers, the register command, a .git/ guard,
and a backgrounded invocation.
"""
import pytest

from memory_layer.cli.cd_hook import (
    hook_for_shell,
    _HOOK_MARKER_BEGIN,
    _HOOK_MARKER_END,
)

SUPPORTED_SHELLS = ["bash", "zsh", "fish", "powershell"]


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_hook_contains_begin_marker(shell):
    assert _HOOK_MARKER_BEGIN in hook_for_shell(shell)


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_hook_contains_end_marker(shell):
    assert _HOOK_MARKER_END in hook_for_shell(shell)


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_markers_are_ordered(shell):
    hook = hook_for_shell(shell)
    assert hook.index(_HOOK_MARKER_BEGIN) < hook.index(_HOOK_MARKER_END)


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_hook_contains_register_command(shell):
    assert "memory-layer register" in hook_for_shell(shell)


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_hook_passes_quiet_flag(shell):
    assert "--quiet" in hook_for_shell(shell)


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_hook_guards_on_git_dir(shell):
    assert ".git" in hook_for_shell(shell)


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_hook_backgrounds_register(shell):
    """The register call must be backgrounded so it never blocks the prompt."""
    hook = hook_for_shell(shell)
    # All shells use & to background the job
    assert "&" in hook, f"{shell}: register call not backgrounded"


def test_zsh_hook_uses_chpwd_functions():
    hook = hook_for_shell("zsh")
    assert "chpwd_functions" in hook


def test_bash_hook_uses_prompt_command():
    hook = hook_for_shell("bash")
    assert "PROMPT_COMMAND" in hook


def test_fish_hook_uses_on_variable_pwd():
    hook = hook_for_shell("fish")
    assert "--on-variable PWD" in hook


def test_powershell_hook_wraps_prompt():
    hook = hook_for_shell("powershell")
    assert "function global:prompt" in hook


def test_unsupported_shell_raises_value_error():
    with pytest.raises(ValueError, match="unsupported shell"):
        hook_for_shell("tcsh")


def test_unsupported_shell_error_includes_name():
    with pytest.raises(ValueError, match="csh"):
        hook_for_shell("csh")


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_hook_each_marker_appears_exactly_once(shell):
    hook = hook_for_shell(shell)
    assert hook.count(_HOOK_MARKER_BEGIN) == 1
    assert hook.count(_HOOK_MARKER_END) == 1
