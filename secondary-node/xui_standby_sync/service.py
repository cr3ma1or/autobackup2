"""systemd service lifecycle operations."""

from __future__ import annotations

import time

from .commands import CommandError, run_command
from .exceptions import ServiceControlError


def stop_service(service_name: str, timeout: int) -> None:
    try:
        run_command(["systemctl", "stop", service_name], timeout=timeout)
    except CommandError as error:
        raise ServiceControlError(
            f"Failed to stop service {service_name}: {error}"
        ) from error


def wait_service_active(service_name: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run_command(
            ["systemctl", "is-active", "--quiet", service_name],
            timeout=min(5, max(1, timeout)),
            check=False,
        )
        if result.returncode == 0:
            return True
        time.sleep(1)
    return False


def start_service(service_name: str, timeout: int) -> None:
    try:
        run_command(["systemctl", "start", service_name], timeout=timeout)
    except CommandError as error:
        raise ServiceControlError(
            f"Failed to start service {service_name}: {error}"
        ) from error
    if not wait_service_active(service_name, timeout):
        raise ServiceControlError(f"Service did not become active: {service_name}")
