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
    "MAX_AGE_SECONDS",
    "MAX_CLOCK_SKEW_SECONDS",
    "MAX_UNPACK_SIZE_BYTES",
    "MAX_ROLLBACK_COPIES",
}
# Full 40-char OpenPGP v4 fingerprints only. Short/long key IDs are rejected:
# they are vulnerable to collision/spoofing attacks, and the primary side
# (xui-backup.sh, install.sh) already enforces exactly 40 hex chars.
_FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")


def _parse_env_file(path: Path) -> dict[str, str]:
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
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key] = value
    return values


def parse_custom_reserved_ports(value: str | None) -> frozenset[int]:
    if value is None or not value.strip():
        return frozenset()
    ports: set[int] = set()
    for raw_port in value.split(","):
        text = raw_port.strip()
        if not text.isdigit():
            raise ConfigurationError(f"Invalid reserved port: {text!r}")
        port = int(text)
        if not 1 <= port <= 65535:
            raise ConfigurationError(f"Reserved port outside 1..65535: {port}")
        ports.add(port)
    return frozenset(ports)


def parse_fingerprints(value: str | None, *, required: bool) -> frozenset[str]:
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
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Invalid boolean value: {value!r}")


def _parse_positive_int(name: str, value: str | None, default: int) -> int:
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
    merged = dict(values)
    if cli_overrides:
        merged.update(cli_overrides)
    require_signature = _parse_bool(merged.get("REQUIRE_GPG_SIGNATURE"), True)
    paths = PathsConfig(
        target_db=TARGET_DB_PATH,
        allowlist_path=ALLOWLIST_PATH,
        incoming_dir=INCOMING_DIR,
        work_root=WORK_DIR,
        snapshots_dir=RUN_SNAPSHOTS_DIR,
        gnupg_dir=GNUPG_DIR,
        sync_lock_path=SYNC_LOCK_FILE,
        store_lock_path=STORE_LOCK_FILE,
        standby_mode_file=STANDBY_MODE_FILE,
        failover_lock_file=FAILOVER_LOCK_FILE,
        log_file=LOG_FILE,
    )
    security = SecurityConfig(
        trusted_gpg_signer_fingerprints=parse_fingerprints(
            merged.get("TRUSTED_GPG_SIGNER_FINGERPRINTS"),
            required=require_signature,
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
