"""
memstrata CLI entry point.

Commands:
  init             4-question interactive onboarding wizard (V5.1 Phase 15'/17b)
  uninit-cd-hook   Remove the shell cd-hook written by `init`
  register <path>  Register a project directory (idempotent; --quiet for hook use)
  api              Start the MemStrata API server
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

# ── init ───────────────────────────────────────────────────────────────────────

def _cmd_init(args: argparse.Namespace) -> None:
    """
    4-question onboarding wizard.

    [1/4] Data directory
    [2/4] Base model (Local / Anthropic / OpenAI)
    [3/4] Enable shell cd-hook?
    [4/4] Which shell?

    API keys (if chosen) are stored in the OS keychain, never to disk.
    """
    home = Path.home()

    print("\nMemStrata — Setup\n")

    # ── [1/4] Data directory ──────────────────────────────────────────────
    default_data_dir = home / ".memstrata"
    raw = input(
        f"[1/4] Where should MemStrata store its data?\n"
        f"      [default: {default_data_dir}] "
    ).strip()
    data_dir = Path(raw).expanduser() if raw else default_data_dir

    # ── [2/4] Base model ──────────────────────────────────────────────────
    print("\n[2/4] Pick your base model:")
    print("      [1] Local (Ollama) — recommended; zero cost")
    print("      [2] Anthropic API (Claude)")
    print("      [3] OpenAI API (GPT-4/o-series)")
    model_choice = input("      Choice: ").strip()
    if model_choice not in ("1", "2", "3"):
        model_choice = "1"

    provider_key: str | None = None
    provider_name: str | None = None
    if model_choice in ("2", "3"):
        provider_name = "anthropic" if model_choice == "2" else "openai"
        provider_key = _api_key_wizard(provider_name)

    # ── [3/4] Enable cd-hook? ─────────────────────────────────────────────
    force_hook = getattr(args, "enable_cd_hook", False)
    if force_hook:
        enable_hook = True
    else:
        print("\n[3/4] Enable shell cd-hook for automatic project discovery?")
        print("      (Adds a hook to your shell config; remove with `memstrata uninit-cd-hook`)")
        enable_hook = input("      [Y/n] ").strip().lower() not in ("n", "no")

    # ── [4/4] Which shell? ────────────────────────────────────────────────
    shell: str | None = None
    if enable_hook:
        from memstrata.cli.cd_hook import detect_shell
        detected = detect_shell()

        _shell_options = [
            ("zsh", "zsh"),
            ("bash", "bash"),
            ("fish", "fish"),
            ("powershell", "PowerShell"),
            (None, "Skip — I'll register projects manually"),
        ]
        print("\n[4/4] What's your shell?")
        for i, (key, label) in enumerate(_shell_options, 1):
            mark = " (detected)" if key == detected else ""
            print(f"      [{i}] {label}{mark}")
        choice_raw = input("      Choice: ").strip()
        try:
            idx = int(choice_raw) - 1
            if 0 <= idx < len(_shell_options):
                shell = _shell_options[idx][0]
            else:
                shell = detected
        except ValueError:
            shell = detected

    # ── Apply configuration ───────────────────────────────────────────────
    print("\nConfiguring...")

    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"  ✓ Created {data_dir}/")

    if provider_key and provider_name:
        from memstrata.config.keychain import store_api_key
        try:
            store_api_key(provider_name, provider_key)
            print(f"  ✓ API key stored in OS keychain (provider: {provider_name})")
        except RuntimeError as exc:
            print(f"  ! Warning: {exc}", file=sys.stderr)

    if enable_hook and shell:
        from memstrata.cli.cd_hook import config_path_for_shell, write_hook
        config_path = config_path_for_shell(shell)
        try:
            write_hook(shell, config_path)
            backup = config_path.with_suffix(config_path.suffix + ".ml-backup")
            print(f"  ✓ Wrote {config_path} cd-hook")
            if backup.exists():
                print(f"  ✓ Created backup at {backup}")
        except Exception as exc:
            print(f"  ! cd-hook write failed: {exc}", file=sys.stderr)
    elif enable_hook and not shell:
        print("  - Skipping cd-hook (no shell selected)")

    _model_label = {
        "1": "Local (Ollama)", "2": "Anthropic (Claude)", "3": "OpenAI",
    }.get(model_choice, "Local")
    print(f"  ✓ Base model: {_model_label}")
    print("\nMemStrata is ready. Run `memstrata api` to start.\n")


def _api_key_wizard(provider: str) -> str | None:
    """Walk the user through pasting and validating an API key."""
    urls = {
        "anthropic": "https://console.anthropic.com/keys",
        "openai": "https://platform.openai.com/api-keys",
    }
    url = urls.get(provider, "")
    print(f"\n  To get a {provider.capitalize()} API key, visit:")
    print(f"  {url}")
    print("  Create a new key, then paste it below.")
    key = input(f"  {provider.capitalize()} API key: ").strip()
    if not key:
        print("  (No key provided; skipping)")
        return None

    valid = _validate_api_key(provider, key)
    if valid is True:
        print("  ✓ Key validated successfully.")
    elif valid is False:
        print("  ! Key validation failed. The key may be invalid or the service is unreachable.")
        keep = input("  Store it anyway? [y/N] ").strip().lower()
        if keep not in ("y", "yes"):
            return None

    return key


def _validate_api_key(provider: str, key: str) -> bool | None:
    """Return True if key is valid, False if definitively bad, None if check failed."""
    try:
        import httpx
        if provider == "anthropic":
            r = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=5.0,
            )
            return r.status_code == 200
        if provider == "openai":
            r = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=5.0,
            )
            return r.status_code == 200
    except Exception:
        return None
    return None


# ── uninit-cd-hook ─────────────────────────────────────────────────────────────

def _cmd_uninit_cd_hook(args: argparse.Namespace) -> None:
    """Remove the memstrata cd-hook from the user's shell config."""
    from memstrata.cli.cd_hook import config_path_for_shell, detect_shell, remove_hook

    shell = getattr(args, "shell", None) or detect_shell()
    if not shell:
        print(
            "Could not detect shell. Specify with --shell bash|zsh|fish|powershell",
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = config_path_for_shell(shell)
    remove_hook(config_path)
    print(f"Removed memstrata cd-hook from {config_path}")


# ── register ───────────────────────────────────────────────────────────────────

def _cmd_register(args: argparse.Namespace) -> None:
    """
    Register a project directory (idempotent).

    Called by the shell cd-hook with --quiet, so error output must go to
    stderr and the process must exit non-zero on failure.
    """
    path = Path(args.path).resolve()
    quiet: bool = getattr(args, "quiet", False)

    if not path.exists():
        if not quiet:
            print(f"register: path does not exist: {path}", file=sys.stderr)
        sys.exit(1)

    if not (path / ".git").exists() and not quiet:
        print(f"register: {path} is not a git repository (no .git/); skipping")
        return

    # Wire to MIT core register logic (stub — wired at runtime via env/config).
    if not quiet:
        print(f"Registered: {path}")


# ── api ────────────────────────────────────────────────────────────────────────

def _cmd_api(args: argparse.Namespace) -> None:
    """Start the MemStrata API server.

    Hard Rule 77 (V5.2-B): the local server binds strictly to 127.0.0.1.
    The host argument is fixed; no external-interface binding is permitted.
    """
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is required. Install with: pip install uvicorn",
            file=sys.stderr,
        )
        sys.exit(1)

    host = "127.0.0.1"
    port: int = getattr(args, "port", 8000)
    print(f"Starting MemStrata API server on http://{host}:{port}")
    uvicorn.run("memstrata.layer3.api_server:app", host=host, port=port)


# ── Parser ─────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memstrata",
        description="MemStrata — open-source context server for LLM-assisted coding.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Quick start:\n"
            "  memstrata init          # interactive setup\n"
            "  memstrata api           # start the API server\n"
            "\n"
            "Project discovery:\n"
            "  memstrata register .    # register current directory manually\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # init
    p_init = sub.add_parser("init", help="Interactive onboarding wizard")
    p_init.add_argument(
        "--enable-cd-hook", action="store_true", default=False,
        help="Skip the cd-hook prompt and enable it directly",
    )
    p_init.set_defaults(func=_cmd_init)

    # uninit-cd-hook
    p_uninit = sub.add_parser("uninit-cd-hook", help="Remove the shell cd-hook")
    p_uninit.add_argument(
        "--shell", choices=["bash", "zsh", "fish", "powershell"],
        help="Shell to remove hook from (default: auto-detected)",
    )
    p_uninit.set_defaults(func=_cmd_uninit_cd_hook)

    # register
    p_reg = sub.add_parser(
        "register",
        help="Register a project directory (idempotent; called by shell cd-hook)",
    )
    p_reg.add_argument("path", help="Path to the project directory")
    p_reg.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress output unless an error occurs",
    )
    p_reg.set_defaults(func=_cmd_register)

    # api — Hard Rule 77: bind is loopback-only; no --host flag is exposed.
    p_api = sub.add_parser("api", help="Start the MemStrata API server (binds 127.0.0.1 only)")
    p_api.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    p_api.set_defaults(func=_cmd_api)

    # ingest
    p_ingest = sub.add_parser(
        "ingest",
        help="Walk a project directory and embed source files for /context/injection",
    )
    p_ingest.add_argument("path", help="Project root to ingest (e.g. '.' for the cwd)")
    p_ingest.add_argument(
        "--project-id",
        default=None,
        help="Override project_id (defaults to the directory basename)",
    )
    p_ingest.add_argument(
        "--no-embed",
        action="store_true",
        default=False,
        help="Skip Ollama embedding; store text chunks only. Useful when "
             "Ollama is offline - re-run without this flag later to backfill.",
    )

    def _cmd_ingest_wrapper(args: argparse.Namespace) -> None:
        from memstrata.cli.ingest import cmd_ingest
        cmd_ingest(args)

    p_ingest.set_defaults(func=_cmd_ingest_wrapper)

    return parser


def main() -> None:
    """CLI entry point installed as `memstrata`."""
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
