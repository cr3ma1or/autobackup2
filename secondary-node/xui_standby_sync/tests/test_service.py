"""Service lifecycle tests."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from xui_standby_sync.exceptions import ServiceControlError
from xui_standby_sync.service import start_service, stop_service, wait_service_active


class TestStopService:
    def test_stop_calls_systemctl(self):
        with patch("xui_standby_sync.service.run_command") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
            stop_service("x-ui", 30)
            mock.assert_called_once_with(["systemctl", "stop", "x-ui"], timeout=30)

    def test_stop_raises_on_failure(self):
        from xui_standby_sync.commands import CommandError
        with patch("xui_standby_sync.service.run_command") as mock:
            mock.side_effect = CommandError("systemctl failed")
            with pytest.raises(ServiceControlError, match="Failed to stop"):
                stop_service("x-ui", 30)


class TestStartService:
    def test_start_calls_systemctl_then_polls(self):
        results = [
            MagicMock(returncode=0),  # start
            MagicMock(returncode=0),  # is-active
        ]
        with patch("xui_standby_sync.service.run_command", side_effect=results):
            start_service("x-ui", 30)

    def test_start_raises_if_never_active(self):
        never_active = MagicMock(returncode=1)
        with (
            patch("xui_standby_sync.service.run_command") as mock_run,
            patch("xui_standby_sync.service.time") as mock_time,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            # Make monotonic expire immediately
            mock_time.monotonic.side_effect = [0.0, 100.0, 100.0]
            mock_time.sleep = MagicMock()
            # is-active always fails
            def side_effect(cmd, **kwargs):
                if "is-active" in cmd:
                    return MagicMock(returncode=1)
                return MagicMock(returncode=0)
            mock_run.side_effect = side_effect

            with pytest.raises(ServiceControlError, match="did not become active"):
                start_service("x-ui", 1)

    def test_start_raises_on_systemctl_failure(self):
        from xui_standby_sync.commands import CommandError
        with patch("xui_standby_sync.service.run_command") as mock:
            mock.side_effect = CommandError("start failed")
            with pytest.raises(ServiceControlError, match="Failed to start"):
                start_service("x-ui", 30)


class TestWaitServiceActive:
    def test_returns_true_when_active(self):
        with (
            patch("xui_standby_sync.service.run_command") as mock_run,
            patch("xui_standby_sync.service.time") as mock_time,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mock_time.monotonic.side_effect = [0.0, 1.0]
            mock_time.sleep = MagicMock()
            result = wait_service_active("x-ui", 30)
            assert result is True

    def test_returns_false_on_timeout(self):
        with (
            patch("xui_standby_sync.service.run_command") as mock_run,
            patch("xui_standby_sync.service.time") as mock_time,
        ):
            mock_run.return_value = MagicMock(returncode=1)
            mock_time.monotonic.side_effect = [0.0, 1.0, 2.0, 100.0]
            mock_time.sleep = MagicMock()
            result = wait_service_active("x-ui", 1)
            assert result is False
