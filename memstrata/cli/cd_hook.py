"""
Shell cd-hook generation and idempotent installation.

Hook text and write/remove patterns taken verbatim from
v5_1_reference/critical_snippets.py §2. The idempotent marker pair
ensures repeated writes replace rather than duplicate the block.

Hard Rule 54: hooks only check for .git/ — no process scanning.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_HOOK_MARKER_BEGIN = "# >>> memstrata cd-hook >>>"
_HOOK_MARKER_END   = "# <<< memstrata cd-hook <<<"


def hook_for_shell(shell: str) -> str:
    """
    Generate the hook block for the given shell.

    The returned string is delimited by _HOOK_MARKER_BEGIN / _HOOK_MARKER_END
    so write_hook can replace it idempotently.
    """
    if shell == "zsh":
        body = """
ml_cd_hook() {
    if [ -d ".git" ] && command -v memstrata >/dev/null 2>&1; then
        (memstrata register "$PWD" --quiet >/dev/null 2>&1 &)
    fi
}
typeset -gaU chpwd_functions
chpwd_functions+=(ml_cd_hook)
"""
    elif shell == "bash":
        body = """
ml_cd_hook() {
    if [ -d ".git" ] && command -v memstrata >/dev/null 2>&1; then
        (memstrata register "$PWD" --quiet >/dev/null 2>&1 &)
    fi
}
PROMPT_COMMAND="ml_cd_hook;${PROMPT_COMMAND:-:}"
"""
    elif shell == "fish":
        body = """
function ml_cd_hook --on-variable PWD
    if test -d .git
        if command -v memstrata >/dev/null 2>&1
            memstrata register "$PWD" --quiet >/dev/null 2>&1 &
        end
    end
end
"""
    elif shell == "powershell":
        body = """
$global:__MlOriginalPrompt = if (Test-Path Function:prompt) { Get-Item Function:prompt } else { $null }
function global:prompt {
    if (Test-Path -PathType Container ".git") {
        if (Get-Command memstrata -ErrorAction SilentlyContinue) {
            Start-Job -ScriptBlock {
                param($p) memstrata register $p --quiet
            } -ArgumentList $PWD.Path | Out-Null
        }
    }
    if ($global:__MlOriginalPrompt) { & $global:__MlOriginalPrompt }
    else { "PS $($executionContext.SessionState.Path.CurrentLocation)$('>' * ($nestedPromptLevel + 1)) " }
}
"""
    else:
        raise ValueError(f"unsupported shell: {shell!r}")

    return f"\n{_HOOK_MARKER_BEGIN}\n{body.strip()}\n{_HOOK_MARKER_END}\n"


def write_hook(shell: str, config_path: Path) -> None:
    """
    Idempotently install the hook into config_path.

    If the marker block is already present it is replaced in-place.
    Otherwise the block is appended. A .ml-backup is created once on
    the first write (never overwritten on subsequent writes).
    """
    backup = config_path.with_suffix(config_path.suffix + ".ml-backup")
    if config_path.exists() and not backup.exists():
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")

    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    new_block = hook_for_shell(shell)

    if _HOOK_MARKER_BEGIN in existing:
        before, _, rest = existing.partition(_HOOK_MARKER_BEGIN)
        _, _, after = rest.partition(_HOOK_MARKER_END)
        after = after.lstrip("\n")
        result = before.rstrip() + new_block + ("\n" + after if after else "")
    else:
        # new_block already starts with "\n", so rstrip() + new_block gives one separator.
        result = existing.rstrip() + new_block

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(result, encoding="utf-8")


def remove_hook(config_path: Path) -> None:
    """
    Reverse write_hook. Strips the marker block from config_path in-place.
    No-op if the file is missing or the block was never written.
    """
    if not config_path.exists():
        return
    text = config_path.read_text(encoding="utf-8")
    if _HOOK_MARKER_BEGIN not in text:
        return
    before, _, rest = text.partition(_HOOK_MARKER_BEGIN)
    _, _, after = rest.partition(_HOOK_MARKER_END)
    config_path.write_text(before.rstrip() + "\n" + after.lstrip("\n"), encoding="utf-8")


def detect_shell() -> str | None:
    """Best-effort shell detection from the environment."""
    shell_env = os.environ.get("SHELL", "")
    if "zsh" in shell_env:
        return "zsh"
    if "bash" in shell_env:
        return "bash"
    if "fish" in shell_env:
        return "fish"
    if os.environ.get("PSModulePath") and not shell_env:
        return "powershell"
    return None


def config_path_for_shell(shell: str) -> Path:
    """Return the canonical config file path for the given shell."""
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        return home / ".bashrc"
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    if shell == "powershell":
        if sys.platform == "win32":
            return home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        return home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1"
    raise ValueError(f"unsupported shell: {shell!r}")
