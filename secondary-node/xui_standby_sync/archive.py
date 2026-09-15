"""GPG decryption, signature validation, and safe SQLite archive extraction."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import shutil
import stat
import tarfile
import zlib
from pathlib import Path
from typing import Any

from .commands import CommandError, run_command
from .constants import DIRECTORY_MODE, FILE_MODE
from .exceptions import ArchiveValidationError, SignatureVerificationError
from .models import SecurityConfig
from .security import (
    best_effort_wipe_file,
    safe_clean_work_dir,
    verify_directory_chain,
    verify_directory_security,
)


def _parse_gpg_status(output: str, trusted: frozenset[str], *, required: bool) -> str:
    """Parse GPG status-fd output and validate signature.
    
    CRITICAL FIX D3: GOODSIG indicates successful decryption AND valid signature.
    VALIDSIG alone is not sufficient, especially with --trust-model always,
    as it may be present even during decryption failures.
    """
    valid_fingerprints: set[str] = set()
    good_signature = False
    for line in output.splitlines():
        if not line.startswith("[GNUPG:] "):
            continue
        fields = line.removeprefix("[GNUPG:] ").split()
        if not fields:
            continue
        if fields[0] == "GOODSIG":
            good_signature = True
        elif fields[0] == "VALIDSIG" and len(fields) >= 2:
            valid_fingerprints.add(fields[1].upper())
    trusted_matches = valid_fingerprints & set(trusted)
    
    # CRITICAL FIX: Require GOODSIG unconditionally when required=True
    # VALIDSIG alone does not guarantee successful decryption
    if not good_signature:
        if required:
            raise SignatureVerificationError(
                "GPG signature is missing or invalid (GOODSIG not found in output)"
            )
        return ""
    
    # After confirming GOODSIG, verify it comes from a trusted signer
    if not trusted_matches:
        raise SignatureVerificationError(
            "GPG signature is valid but signed by an untrusted fingerprint"
        )
    return sorted(trusted_matches)[0]


_ALLOWED_ARCHIVE_NAMES = frozenset({
    "manifest.json",
    "x-ui.db",
    "3xui_export.json",
    "./manifest.json",
    "./x-ui.db",
    "./3xui_export.json",
})
_MAX_MANIFEST_SIZE = 1024 * 1024
_COPY_BLOCK_SIZE = 1024 * 1024


def _extract_worker(
    payload_path: str,
    work_dir: str,
    max_size: int,
    result_queue: multiprocessing.Queue[str],
) -> None:
    try:
        with tarfile.open(payload_path, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise ArchiveValidationError("Archive is empty")
            for member in members:
                if not member.isreg():
                    raise ArchiveValidationError(
                        "Archive members must be regular files"
                    )
                if member.name not in _ALLOWED_ARCHIVE_NAMES:
                    raise ArchiveValidationError(
                        f"Unexpected archive member: {member.name}"
                    )
            dbs = [
                m for m in members if m.name in {"x-ui.db", "./x-ui.db"}
            ]
            if len(dbs) != 1:
                raise ArchiveValidationError(
                    "Archive must contain exactly one x-ui.db member"
                )
            db_member = dbs[0]
            if not 0 < db_member.size <= max_size:
                raise ArchiveValidationError(
                    "Archive member size is outside allowed range"
                )
            manifest_member = next(
                (m for m in members if m.name == "manifest.json"), None
            )
            manifest: dict[str, Any] | None = None
            if manifest_member is not None:
                if not 0 <= manifest_member.size <= _MAX_MANIFEST_SIZE:
                    raise ArchiveValidationError("manifest.json exceeds maximum size")
                manifest_source = archive.extractfile(manifest_member)
                if manifest_source is not None:
                    try:
                        manifest_bytes = manifest_source.read(_MAX_MANIFEST_SIZE + 1)
                        if len(manifest_bytes) > _MAX_MANIFEST_SIZE:
                            raise ArchiveValidationError(
                                "manifest.json exceeds maximum size"
                            )
                        manifest = json.loads(manifest_bytes)
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        raise ArchiveValidationError(
                            "manifest.json is not valid JSON"
                        ) from error
                    finally:
                        manifest_source.close()
            source = archive.extractfile(db_member)
            if source is None:
                raise ArchiveValidationError("Cannot read archive member")
            destination = Path(work_dir) / "x-ui.db"
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                FILE_MODE,
            )
            class LimitedReader:
                def __init__(self, source: Any, limit: int) -> None:
                    self.source = source
                    self.remaining = limit
                    self.copied = 0

                def read(self, size: int = -1) -> bytes:
                    if self.remaining <= 0:
                        return b""
                    requested = (
                        self.remaining if size < 0 else min(size, self.remaining)
                    )
                    chunk = self.source.read(requested)
                    self.copied += len(chunk)
                    self.remaining -= len(chunk)
                    return chunk

            reader = LimitedReader(source, db_member.size)
            with source, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(reader, output, length=_COPY_BLOCK_SIZE)
                if reader.remaining != 0 or source.read(1):
                    raise ArchiveValidationError("Database member size is invalid")
            copied = reader.copied
            if copied != db_member.size:
                raise ArchiveValidationError(
                    "Extracted data size differs from manifest"
                )
            if manifest is not None:
                expected = manifest.get("database_sha256")
                if not isinstance(expected, str) or len(expected) != 64:
                    raise ArchiveValidationError(
                        "manifest database_sha256 is invalid"
                    )
                try:
                    int(expected, 16)
                except ValueError as error:
                    raise ArchiveValidationError(
                        "manifest database_sha256 is invalid"
                    ) from error
                digest = hashlib.sha256()
                with destination.open("rb") as fh:
                    for block in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest().lower() != expected.lower():
                    raise ArchiveValidationError(
                        "Database hash differs from manifest"
                    )
        result_queue.put("")
    except (
        OSError,
        EOFError,
        ValueError,
        tarfile.TarError,
        zlib.error,
        ArchiveValidationError,
    ) as error:
        result_queue.put(str(error))


def _extract_archive(
    payload_path: Path, work_dir: Path, max_size: int, timeout: int
) -> None:
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue(maxsize=1)
    worker = context.Process(
        target=_extract_worker,
        args=(str(payload_path), str(work_dir), max_size, result_queue),
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        worker.terminate()
        worker.join(5)
        if worker.is_alive():
            worker.kill()
            worker.join()
        raise ArchiveValidationError(f"Archive extraction timed out after {timeout}s")
    if worker.exitcode != 0:
        raise ArchiveValidationError(
            f"Archive worker exited with code {worker.exitcode}"
        )
    try:
        result = result_queue.get(timeout=1)
    except queue.Empty as error:
        raise ArchiveValidationError("Archive worker returned no result") from error
    finally:
        result_queue.close()
        result_queue.join_thread()
    if not isinstance(result, str):
        raise ArchiveValidationError("Archive worker returned an invalid result")
    if result:
        raise ArchiveValidationError(result)


def decrypt_and_extract(
    *,
    archive_path: Path,
    work_dir: Path,
    gnupg_dir: Path,
    security: SecurityConfig,
    timeout: int,
    terminate_gpg_agent: bool = True,
) -> tuple[Path, str]:
    """Decrypt and extract one verified DB; always attempt gpg-agent cleanup."""
    verify_directory_security(gnupg_dir, "GPG home")
    verify_directory_chain(gnupg_dir.parent, "GPG home")
    for item in gnupg_dir.iterdir():
        item_stat = item.lstat()
        if stat.S_ISSOCK(item_stat.st_mode):
            continue
        if item.is_symlink() or item_stat.st_uid != 0 or item_stat.st_mode & 0o077:
            raise ArchiveValidationError(f"Unsafe GPG home entry: {item}")
        if item.is_dir() and (item_stat.st_mode & 0o777) != DIRECTORY_MODE:
            raise ArchiveValidationError(f"Unsafe GPG home directory: {item}")

    safe_clean_work_dir(work_dir, safe_root=work_dir.parent)
    work_dir.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    payload_path = work_dir / "payload.tar.gz"
    env = os.environ.copy()
    env["GNUPGHOME"] = str(gnupg_dir)
    command = [
        "gpg",
        "--batch",
        "--yes",
        "--status-fd",
        "1",
        "--decrypt",
        "--output",
        str(payload_path),
        str(archive_path),
    ]
    signer = ""
    try:
        result = run_command(command, timeout=timeout, env=env, check=True)
        signer = _parse_gpg_status(
            result.stdout,
            security.trusted_gpg_signer_fingerprints,
            required=security.require_gpg_signature,
        )
        _extract_archive(
            payload_path, work_dir, security.max_unpack_size_bytes, timeout
        )
        extracted = work_dir / "x-ui.db"
        if not extracted.is_file() or extracted.is_symlink():
            raise ArchiveValidationError("Extracted x-ui.db is missing or unsafe")
        return extracted, signer
    finally:
        try:
            if terminate_gpg_agent:
                run_command(
                    ["gpgconf", "--homedir", str(gnupg_dir), "--kill", "gpg-agent"],
                    timeout=min(timeout, 10),
                    check=False,
                )
        except CommandError as error:
            import logging

            logging.getLogger(__name__).warning(
                "gpg-agent cleanup failed: %s", error
            )
        finally:
            best_effort_wipe_file(payload_path, timeout=timeout)
