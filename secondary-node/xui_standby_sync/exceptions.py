"""Domain exceptions for controlled synchronization failures."""


class SyncError(Exception):
    """Base error for a controlled synchronization failure."""


class SecurityViolationError(SyncError):
    """A privileged file, directory, or boundary failed validation."""


class LockBusyError(SyncError):
    """A required advisory lock is already held."""


class ConfigurationError(SyncError):
    """Configuration is missing, malformed, or contradictory."""


class BackupValidationError(SyncError):
    """Backup path, checksum, freshness, or provenance is invalid."""


class SignatureVerificationError(BackupValidationError):
    """Backup signature is missing, invalid, or untrusted."""


class ArchiveValidationError(BackupValidationError):
    """Archive contents are invalid or unsafe to extract."""


class IntegrityCheckError(SyncError):
    """SQLite integrity or invariant validation failed."""


class SchemaValidationError(SyncError):
    """Database schema is missing or incompatible."""


class PlanValidationError(SyncError):
    """A database synchronization plan is invalid."""


class PlanExecutionError(SyncError):
    """A validated synchronization plan could not be applied."""


class ServiceControlError(SyncError):
    """systemd service control or health verification failed."""


class RollbackError(SyncError):
    """Rollback snapshot creation or restoration failed."""


class OperationCancelledError(SyncError):
    """The operation was cancelled outside a critical recovery phase."""
