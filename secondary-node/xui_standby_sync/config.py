"""Configuration parsing and immutable runtime configuration construction."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from .constants import (
    ALLOWLIST_PATH,
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    DEFAULT_MAX_ROLLBACK_COPIES,
    DEFAULT_MAX_UNPACK_SIZE_BYTES,
    FAILOVER_LOCK_FILE,
    GNUPG_DIR,
    INCOMING_DIR,
    LOG_FILE,
    RUN_SNAPSHOTS_DIR,
    SERVICE_NAME,
    STANDBY_MODE_FILE,
    STORE_LOCK_FILE,
    SYNC_LOCK_FILE,
    TARGET_DB_PATH,
    WORK_DIR,
)
from .exceptions import ConfigurationError
from .models import (
    NotificationConfig,
    PathsConfig,
    RuntimeConfig,
    RuntimeOptions,
    SecurityConfig,
    SyncPolicy,
)
from .security import verify_file_security

_ALLOWED_ENV_KEYS = {
    "SEND_TELEGRAM",
    "TG_BOT_TOKEN",
    "TG_CHAT_ID",
    "TG_PROXY_URL",
    "PRIMARY_IP",
    "STANDBY_IP",
    "CUSTOM_RESERVED_PORTS",
    "TRUSTED_GPG_SIGNER_FINGERPRINTS",
    "REQUIRE_GPG_SIGNATURE",
    "CMD_TIMEOUT",
    "MAX_AGE_DAYS",
    "MAX_AGE_SECONDS",
    "MAX_CLOCK_SKEW_SECONDS",
    "MAX_UNPACK_SIZE_BYTES",
    "MAX_ROLLBACK_COPIES",
    "TARGET_DB_PATH",
    "INCOMING_DIR",
    "WORK_DIR",
    "SNAPSHOTS_DIR",
    "GNUPG_DIR",
    "STANDBY_MODE_FILE",
    "ALLOWLIST_PATH",
}
# Full 40-char OpenPGP v4 fingerprints only. Short/long key IDs are rejected:
# they are vulnerable to collision/spoofing attacks, and the primary side
# (xui-backup.sh, install.sh) already enforces exactly 40 hex chars.
_FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        path.resolve().relative_to(Path("/etc"))
    except ValueError:
        pass
    else:
        verify_file_security(path, "configuration file")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(
            f"Cannot read configuration file {path}: {error}"
        ) from error
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)$", stripped)
        if not match:
            raise ConfigurationError(f"Malformed configuration line in {path}")
        key, value = match.groups()
        if key in _ALLOWED_ENV_KEYS:
            value = value.strip()
            if value[:1] in {"'", '"'}:
                if len(value) < 2 or value[-1] != value[0]:
                    raise ConfigurationError(f"Unbalanced quotes for {key}")
                value = value[1:-1]
            elif value[-1:] in {"'", '"'}:
                raise ConfigurationError(f"Unbalanced quotes for {key}")
            values[key] = value
    return values


def parse_custom_reserved_ports(value: str | None) -> frozenset[int]:
    if value is not None and not isinstance(value, str):
        raise ConfigurationError("CUSTOM_RESERVED_PORTS must be a string")
    if value is None or not value.strip():
        return frozenset()
    ports: set[int] = set()
    for raw_port in value.split(","):
        text = raw_port.strip()
        try:
            port = int(text)
        except ValueError as error:
            raise ConfigurationError(
                f"Invalid reserved port: {text!r}"
            ) from error
        if not 1 <= port <= 65535:
            raise ConfigurationError(f"Reserved port outside 1..65535: {port}")
        ports.add(port)
    return frozenset(ports)


def parse_fingerprints(value: str | None, *, required: bool) -> frozenset[str]:
    if value is not None and not isinstance(value, str):
        raise ConfigurationError("TRUSTED_GPG_SIGNER_FINGERPRINTS must be a string")
    if not isinstance(required, bool):
        raise ConfigurationError("required must be a boolean")
    if value is None or not value.strip():
        if required:
            raise ConfigurationError("Trusted GPG signer fingerprints are required")
        return frozenset()
    fingerprints: set[str] = set()
    for raw_fingerprint in value.split(","):
        fingerprint = re.sub(r"\s+", "", raw_fingerprint).upper()
        if not _FINGERPRINT_RE.fullmatch(fingerprint):
            raise ConfigurationError(
                f"Invalid GPG signer fingerprint (must be a full 40-char "
                f"hex fingerprint): {raw_fingerprint!r}"
            )
        fingerprints.add(fingerprint)
    return frozenset(fingerprints)


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is not None and not isinstance(value, str):
        raise ConfigurationError("Boolean configuration values must be strings")
    if not isinstance(default, bool):
        raise ConfigurationError("Boolean default must be a boolean")
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Invalid boolean value: {value!r}")


def _parse_positive_int(name: str, value: str | None, default: int) -> int:
    if not isinstance(name, str) or not isinstance(default, int):
        raise ConfigurationError("Invalid integer parser arguments")
    if value is not None and not isinstance(value, str):
        raise ConfigurationError(f"{name} must be a string")
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(f"Invalid integer for {name}: {value!r}") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return parsed


def load_values(config_path: Path | None = None) -> dict[str, str]:
    """Compose .env values with required precedence before CLI overrides."""
    values: dict[str, str] = {}
    for path in (Path("/etc/x-ui/.env"), Path("/etc/x-ui/sync.env"), config_path):
        if path is not None and path.is_file():
            values.update(_parse_env_file(path))
    for key in _ALLOWED_ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def _path_override(values: Mapping[str, str], key: str, default: Path) -> Path:
    value = values.get(key)
    return default if value is None else Path(value).resolve()


def build_runtime_config(
    values: Mapping[str, str],
    *,
    force: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
    verbose: bool = False,
    unsafe_backup_path: bool = False,
    cli_overrides: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    if not isinstance(values, Mapping):
        raise ConfigurationError("Configuration values must be a mapping")
    if cli_overrides is not None and not isinstance(cli_overrides, Mapping):
        raise ConfigurationError("CLI overrides must be a mapping")
    merged = dict(values)
    if cli_overrides:
        merged.update(cli_overrides)
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in merged.items()
    ):
        raise ConfigurationError("Configuration keys and values must be strings")
    if "MAX_AGE_DAYS" in merged and "MAX_AGE_SECONDS" in merged:
        raise ConfigurationError(
            "Set only one retention key: MAX_AGE_DAYS or MAX_AGE_SECONDS"
        )
    if "MAX_AGE_DAYS" in merged:
        merged["MAX_AGE_SECONDS"] = str(
            _parse_positive_int("MAX_AGE_DAYS", merged["MAX_AGE_DAYS"], 1) * 86400
        )
    require_signature = _parse_bool(merged.get("REQUIRE_GPG_SIGNATURE"), True)
    paths = PathsConfig(
        target_db=_path_override(merged, "TARGET_DB_PATH", TARGET_DB_PATH),
        allowlist_path=_path_override(merged, "ALLOWLIST_PATH", ALLOWLIST_PATH),
        incoming_dir=_path_override(merged, "INCOMING_DIR", INCOMING_DIR),
        work_root=_path_override(merged, "WORK_DIR", WORK_DIR),
        snapshots_dir=_path_override(merged, "SNAPSHOTS_DIR", RUN_SNAPSHOTS_DIR),
        gnupg_dir=_path_override(merged, "GNUPG_DIR", GNUPG_DIR),
        sync_lock_path=SYNC_LOCK_FILE,
        store_lock_path=STORE_LOCK_FILE,
        standby_mode_file=_path_override(
            merged, "STANDBY_MODE_FILE", STANDBY_MODE_FILE
        ),
        failover_lock_file=FAILOVER_LOCK_FILE,
        log_file=LOG_FILE,
    )
    security = SecurityConfig(
        trusted_gpg_signer_fingerprints=parse_fingerprints(
            merged.get("TRUSTED_GPG_SIGNER_FINGERPRINTS"),
            required=False,
        ),
        require_gpg_signature=require_signature,
        max_unpack_size_bytes=_parse_positive_int(
            "MAX_UNPACK_SIZE_BYTES",
            merged.get("MAX_UNPACK_SIZE_BYTES"),
            DEFAULT_MAX_UNPACK_SIZE_BYTES,
        ),
        allow_unsafe_backup_path=unsafe_backup_path,
    )
    policy = SyncPolicy(
        service_name=SERVICE_NAME,
        command_timeout=_parse_positive_int(
            "CMD_TIMEOUT", merged.get("CMD_TIMEOUT"), DEFAULT_CMD_TIMEOUT
        ),
        max_age_seconds=_parse_positive_int(
            "MAX_AGE_SECONDS", merged.get("MAX_AGE_SECONDS"), DEFAULT_MAX_AGE_SECONDS
        ),
        max_clock_skew_seconds=_parse_positive_int(
            "MAX_CLOCK_SKEW_SECONDS",
            merged.get("MAX_CLOCK_SKEW_SECONDS"),
            DEFAULT_MAX_CLOCK_SKEW_SECONDS,
        ),
        max_rollback_copies=_parse_positive_int(
            "MAX_ROLLBACK_COPIES",
            merged.get("MAX_ROLLBACK_COPIES"),
            DEFAULT_MAX_ROLLBACK_COPIES,
        ),
        custom_reserved_ports=parse_custom_reserved_ports(
            merged.get("CUSTOM_RESERVED_PORTS")
        ),
        primary_ip=merged.get("PRIMARY_IP") or None,
        standby_ip=merged.get("STANDBY_IP") or None,
    )
    notifications = NotificationConfig(
        enabled=_parse_bool(merged.get("SEND_TELEGRAM"), False),
        bot_token=merged.get("TG_BOT_TOKEN") or None,
        chat_id=merged.get("TG_CHAT_ID") or None,
        proxy_url=merged.get("TG_PROXY_URL") or None,
    )
    return RuntimeConfig(
        paths=paths,
        security=security,
        policy=policy,
        notifications=notifications,
        options=RuntimeOptions(
            force=force,
            dry_run=dry_run,
            json_output=json_output,
            verbose=verbose,
            unsafe_backup_path=unsafe_backup_path,
        ),
    )
