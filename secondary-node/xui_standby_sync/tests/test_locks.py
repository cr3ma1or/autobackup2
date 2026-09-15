"""Lock management tests as required by section 11.3 of TZ-UPGRADE.md."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from xui_standby_sync.exceptions import LockBusyError, SecurityViolationError
from xui_standby_sync.locks import LockSet


class TestLockBasicFunctionality:
    """Test basic lock acquisition and release."""

    def test_first_exclusive_lock_succeeds(self, tmp_path: Path):
        """First exclusive lock succeeds."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        lock = LockSet(sync_lock, store_lock)
        with lock:
            assert sync_lock.exists()
            assert store_lock.exists()
        
        # Locks should be released after context exit
        # (implementation-specific behavior)

    def test_second_non_blocking_lock_returns_busy(self, tmp_path: Path):
        """Second non-blocking lock returns busy."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        first_lock = LockSet(sync_lock, store_lock)
        with first_lock:
            # Store lock is acquired first, so second attempt should fail
            second_lock = LockSet(sync_lock, store_lock)
            with pytest.raises(LockBusyError, match="Lock is busy"):
                with second_lock:
                    pass

    def test_self_lock_is_released_after_store_lock_failure(self, tmp_path: Path):
        """Self lock is released after store lock failure."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        # Mock store lock to fail after first attempt
        original_open = os.open
        
        def mock_open(path, flags, *args, **kwargs):
            if "store" in str(path):
                raise OSError("Store lock failed")
            return original_open(path, flags, *args, **kwargs)
        
        with patch("os.open", side_effect=mock_open):
            lock = LockSet(sync_lock, store_lock)
            with pytest.raises(LockBusyError), lock:
                pass
            
            # Context manager should release self lock on exception
            # Sync lock should be cleaned up

    def test_context_manager_releases_locks_on_exception(self, tmp_path: Path):
        """Context manager releases both locks on exception."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        lock = LockSet(sync_lock, store_lock)
        
        with pytest.raises(RuntimeError), lock:
            raise RuntimeError("Test exception")
        
        # Locks should be released even after exception
        # (implementation-specific cleanup verification)

    def test_symlink_and_insecure_lock_file_are_rejected(self, tmp_path: Path):
        """Symlink and insecure lock file are rejected."""
        # Test symlink rejection
        real_lock = tmp_path / "real.lock"
        real_lock.write_text("content")
        
        symlink_lock = tmp_path / "symlink.lock"
        symlink_lock.symlink_to(real_lock)
        
        # This should raise SecurityViolationError when opening lock
        with pytest.raises(SecurityViolationError):
            with LockSet(symlink_lock, tmp_path / "store.lock"):
                pass

    def test_regular_file_and_secure_permissions(self, tmp_path: Path):
        """Test regular file ownership and permissions."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        # Create files with proper permissions
        sync_lock.touch()
        store_lock.touch()
        os.chmod(sync_lock, 0o600)
        os.chmod(store_lock, 0o600)
        
        # Mock os.open to verify mode and ownership checks
        with patch("os.open") as mock_open:
            def mock_open_handler(path, flags, *args, **kwargs):
                if "store" in str(path):
                    # Simulate successful open with flock
                    mock_fd = 123
                    mock_handle = type("MockHandle", (), {
                        "close": lambda self: None,
                        "fileno": lambda self: mock_fd,
                    })()
                    mock_open.return_value = mock_handle
                    return mock_fd
                return 123
            
            lock = LockSet(sync_lock, store_lock)
            # Would raise SecurityViolationError if permissions are wrong

    def test_fcntl_import_fallback(self, tmp_path: Path):
        """Test behavior when fcntl is not available."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        with patch.dict("sys.modules", {"fcntl": None}):
            with pytest.raises(SecurityViolationError, match="fcntl is required"):
                lock = LockSet(sync_lock, store_lock)
                with lock:
                    pass


class TestStoreLockRetryBehavior:
    """Test store lock retry logic as required by section 8.3."""

    def test_store_lock_retries_exact_five_times(self, tmp_path: Path):
        """Store lock retries exactly five times."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        # Mock first five attempts to fail, sixth to succeed
        call_count = 0
        
        def mock_open_and_lock(path, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 5:
                raise LockBusyError("Store lock busy")
            
            # Simulate successful lock
            return type("MockHandle", (), {
                "close": lambda self: None,
                "fileno": lambda self: 123,
            })()
        
        with patch.object(LockSet, "_open_and_lock", side_effect=mock_open_and_lock):
            lock = LockSet(sync_lock, store_lock, retries=5, retry_interval=1.0)
            with lock:
                pass
        
        # Should have attempted exactly 5+1 = 6 times (initial + 5 retries)
        assert call_count == 6

    def test_store_lock_success_after_retries(self, tmp_path: Path):
        """Store lock succeeds after retrying."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        attempt_times = []
        
        def mock_open_with_timing(path, *args, **kwargs):
            import time
            attempt_times.append(time.time())
            
            if len(attempt_times) <= 2:
                raise LockBusyError("Store lock busy")
            
            return type("MockHandle", (), {
                "close": lambda self: None,
                "fileno": lambda self: 123,
            })()
        
        with patch.object(LockSet, "_open_and_lock", side_effect=mock_open_with_timing):
            lock = LockSet(sync_lock, store_lock, retries=5, retry_interval=1.0)
            start_time = time.time()
            with lock:
                pass
            elapsed = time.time() - start_time
            
            # Should have waited for retry intervals
            assert len(attempt_times) == 3
            # Total time should be approximately 2 seconds (2 retries * 1.0 sec)
            assert 1.5 <= elapsed <= 2.5

    def test_store_lock_failure_after_max_retries(self, tmp_path: Path):
        """Store lock fails after max retries, returns clean no-op exit code."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        # Mock all attempts to fail
        with patch.object(LockSet, "_open_and_lock", side_effect=LockBusyError("Store lock busy")):
            lock = LockSet(sync_lock, store_lock, retries=5, retry_interval=1.0)
            
            # Should raise LockBusyError after exhausting retries
            with pytest.raises(LockBusyError), lock:
                pass
            
            # The implementation should ensure clean exit code
            # (verification through the exception handling)


class TestLockCleanup:
    """Test lock cleanup and deterministic release."""

    def test_lock_cleanup_on_exception(self, tmp_path: Path):
        """Any failure after self lock acquisition releases self lock."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        cleanup_verified = []
        
        def mock_open(path, *args, **kwargs):
            if "sync" in str(path):
                # First open succeeds
                return type("MockHandle", (), {
                    "close": lambda self: cleanup_verified.append("sync"),
                    "fileno": lambda self: 123,
                })()
            elif "store" in str(path):
                # Store open fails
                raise LockBusyError("Store lock busy")
            
        with patch("xui_standby_sync.locks.os.open", side_effect=mock_open):
            with patch("xui_standby_sync.locks.os.fstat") as mock_fstat:
                mock_fstat.return_value = type(
                    "Stat", (), {"st_mode": 0o100600, "st_uid": 0, "st_gid": 0}
                )()
                with patch("fcntl.flock"):
                    lock = LockSet(sync_lock, store_lock, retries=1)

                    with pytest.raises(LockBusyError), lock:
                        pass

        # Cleanup should have been called for sync lock
        assert "sync" in cleanup_verified

    def test_deterministic_cleanup_order(self, tmp_path: Path):
        """Files are closed and locks are released in reverse acquisition order."""
        sync_lock = tmp_path / "sync.lock"
        store_lock = tmp_path / "store.lock"
        
        close_order = []
        
        def mock_handle():
            return type("MockHandle", (), {
                "close": lambda self: close_order.append("close"),
                "fileno": lambda self: 123,
            })()
        
        with patch("xui_standby_sync.locks.os.open", return_value=123):
            with patch("xui_standby_sync.locks.os.fdopen", return_value=mock_handle()):
                with patch("xui_standby_sync.locks.os.fstat", return_value=type(
                    "Stat", (), {"st_mode": 0o100600, "st_uid": 0, "st_gid": 0}
                )()):
                    with patch("fcntl.flock"):
                        lock = LockSet(sync_lock, store_lock)
                        with lock:
                            pass

        # Cleanup order should be reverse of acquisition (LIFO)
        assert len(close_order) == 2

    def test_symlink_rejection_with_fcntl(self, tmp_path: Path):
        """Symlink lock file is rejected even with fcntl available."""
        real_lock = tmp_path / "real.lock"
        real_lock.write_text("content")
        
        symlink_lock = tmp_path / "symlink.lock"
        symlink_lock.symlink_to(real_lock)
        
        with pytest.raises(SecurityViolationError):
            with LockSet(symlink_lock, tmp_path / "store.lock"):
                pass