"""Backup discovery, checksum, timestamp, and freshness validation."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .constants import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    INCOMING_DIR,
)
from .exceptions import BackupValidationError
from .models import BackupMetadata
from .security import (
    validate_backup_file,
    validate_path_inside,
    verify_directory_security,
)

_BACKUP_NAME_RE = re.compile(
    r"^xui-backup-(?P<date>[0-9]{8})T(?P<time>[0-9]{6})Z-.*\.tar\.gz\.gpg$"
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise BackupValidationError(f"Cannot hash backup {path}: {error}") from error
    return digest.hexdigest()


def parse_backup_timestamp(name: str) -> datetime:
    match = _BACKUP_NAME_RE.fullmatch(name)
    if match is None:
        raise BackupValidationError(f"Unsupported backup filename: {name}")
    try:
        return datetime.strptime(
            f"{match.group('date')}{match.group('time')}", "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise BackupValidationError(f"Invalid backup timestamp in {name}") from error


def verify_freshness(
    timestamp: datetime,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    now: float | None = None,
) -> tuple[bool, float]:
    current_time = time.time() if now is None else now
    age_seconds = current_time - timestamp.timestamp()
    if age_seconds < -max_clock_skew_seconds:
        raise BackupValidationError(
            "Backup timestamp exceeds allowed future clock skew"
        )
    age_seconds = max(age_seconds, 0.0)
    return age_seconds <= max_age_seconds, age_seconds / 3600


def _read_sidecar(path: Path) -> str:
    try:
        tokens = path.read_text(encoding="utf-8").strip().split()
    except (OSError, UnicodeDecodeError) as error:
        raise BackupValidationError(
            f"Cannot read checksum sidecar {path}: {error}"
        ) from error
    if not tokens or not _SHA256_RE.fullmatch(tokens[0]):
        raise BackupValidationError(f"Invalid SHA-256 sidecar: {path}")
    return tokens[0].lower()


def verify_checksum(archive_path: Path) -> str:
    sidecar_path = Path(f"{archive_path}.sha256")
    validate_backup_file(archive_path, "backup archive", allow_user_owned=True)
    validate_backup_file(sidecar_path, "backup checksum sidecar", allow_user_owned=True)
    expected = _read_sidecar(sidecar_path)
    actual = calculate_sha256(archive_path)
    if actual != expected:
        raise BackupValidationError(
            f"SHA-256 mismatch for {archive_path.name}: expected {expected}, "
            f"got {actual}"
        )
    return actual


def discover_backup(
    *,
    incoming_dir: Path = INCOMING_DIR,
    override_path: Path | None = None,
    allow_unsafe_path: bool = False,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    now: float | None = None,
) -> BackupMetadata:
    if override_path is not None:
        archive_path = override_path.absolute()
        if not allow_unsafe_path:
            verify_directory_security(
                incoming_dir, "incoming backup directory", allow_user_owned=True
            )
            incoming_root = incoming_dir.resolve()
            archive_path = validate_path_inside(
                archive_path, incoming_root, "backup archive"
            )
        validate_backup_file(archive_path, "backup archive", allow_user_owned=True)
    else:
        verify_directory_security(
            incoming_dir, "incoming backup directory", allow_user_owned=True
        )
        candidates: list[tuple[datetime, Path]] = []
        for candidate in incoming_dir.iterdir():
            if _BACKUP_NAME_RE.fullmatch(candidate.name):
                try:
                    validate_backup_file(
                        candidate, "backup archive", allow_user_owned=True
                    )
                    candidates.append(
                        (parse_backup_timestamp(candidate.name), candidate)
                    )
                except BackupValidationError:
                    continue
        if not candidates:
            raise BackupValidationError("No valid backup archives found")
        _, archive_path = max(candidates, key=lambda item: item[0])

    timestamp = parse_backup_timestamp(archive_path.name)
    archive_hash = verify_checksum(archive_path)
    is_fresh, age_hours = verify_freshness(
        timestamp,
        max_age_seconds=max_age_seconds,
        max_clock_skew_seconds=max_clock_skew_seconds,
        now=now,
    )
    return BackupMetadata(
        archive_path=archive_path,
        archive_name=archive_path.name,
        timestamp=timestamp,
        age_hours=age_hours,
        sha256_hash=archive_hash,
        is_fresh=is_fresh,
        signer_fingerprint=None,
        signature_verified=False,
    )
