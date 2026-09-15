"""Controlled execution wrapper for external commands."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

from .exceptions import SyncError


class CommandError(SyncError):
    """An external command failed, timed out, or could not be started."""


def _display_command(command: list[str]) -> str:
    return " ".join(
        "<redacted>" if "TOKEN" in part.upper() else part for part in command
    )


def run_command(
    command: list[str],
    *,
    timeout: float,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and with a mandatory positive timeout."""
    if not command:
        raise CommandError("Command cannot be empty")
    if timeout <= 0:
        raise CommandError("Command timeout must be positive")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as error:
        raise CommandError(
            f"Command '{_display_command(command)}' timed out after {timeout}s"
        ) from error
    except (FileNotFoundError, PermissionError, OSError) as error:
        raise CommandError(f"Failed to execute '{command[0]}': {error}") from error

    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise CommandError(
            f"Command '{_display_command(command)}' failed with exit code "
            f"{result.returncode}: {detail}"
        )
    return result
