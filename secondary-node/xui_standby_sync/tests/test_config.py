"""Configuration parsing and validation tests as required by section 8.4 of TZ-UPGRADE.md."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from xui_standby_sync.config import (
    build_runtime_config,
    load_values,
    parse_custom_reserved_ports,
    parse_fingerprints,
)
from xui_standby_sync.constants import ALLOWLIST_PATH, TARGET_DB_PATH
from xui_standby_sync.exceptions import ConfigurationError


class TestCustomReservedPortsParsing:
    """Test parsing of custom reserved ports."""

    def test_empty_value_returns_empty_set(self):
        """Empty value returns empty set."""
        result = parse_custom_reserved_ports(None)
        assert result == frozenset()
        
        result = parse_custom_reserved_ports("")
        assert result == frozenset()

    def test_valid_ports_parsed_correctly(self):
        """Valid ports are parsed correctly."""
        result = parse_custom_reserved_ports("22,80,443")
        assert result == frozenset({22, 80, 443})

    def test_single_port(self):
        """Single port parsing works."""
        result = parse_custom_reserved_ports("3306")
        assert result == frozenset({3306})

    def test_whitespace_is_trimmed(self):
        """Whitespace around ports is trimmed."""
        result = parse_custom_reserved_ports(" 22 , 80 , 443 ")
        assert result == frozenset({22, 80, 443})

    def test_invalid_port_raises_error(self):
        """Non-numeric port raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="Invalid reserved port"):
            parse_custom_reserved_ports("abc")

    def test_port_out_of_range_raises_error(self):
        """Port outside 1..65535 raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="outside 1..65535"):
            parse_custom_reserved_ports("0")
        
        with pytest.raises(ConfigurationError, match="outside 1..65535"):
            parse_custom_reserved_ports("65536")

    def test_negative_port_raises_error(self):
        """Negative port raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="outside 1..65535"):
            parse_custom_reserved_ports("-1")


class TestFingerprintParsing:
    """Test parsing of trusted GPG fingerprints."""

    def test_single_fingerprint(self):
        """Single fingerprint is parsed correctly."""
        result = parse_fingerprints("ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234", required=True)
        assert result == frozenset(["ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"])

    def test_multiple_fingerprints(self):
        """Multiple fingerprints separated by commas."""
        result = parse_fingerprints(
            "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234, "
            "1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD",
            required=True,
        )
        assert len(result) == 2
        assert "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234" in result

    def test_fingerprints_with_spaces(self):
        """Fingerprints with spaces are normalized."""
        result = parse_fingerprints(
            "ABCD 1234 ABCD 1234 ABCD 1234 ABCD 1234 ABCD 1234",
            required=True,
        )
        assert result == frozenset(["ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"])

    def test_short_key_id_rejected(self):
        """Short key IDs are rejected (collision/spoofing risk)."""
        with pytest.raises(ConfigurationError, match="Invalid GPG signer fingerprint"):
            parse_fingerprints("ABCD1234ABCD1234", required=True)

    def test_lowercase_fingerprint_normalized(self):
        """Lowercase fingerprints are normalized to uppercase."""
        result = parse_fingerprints("abcd1234abcd1234abcd1234abcd1234abcd1234", required=True)
        assert result == frozenset(["ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"])

    def test_empty_fingerprint_required_raises_error(self):
        """Empty fingerprint when required raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="Trusted GPG signer fingerprints are required"):
            parse_fingerprints("", required=True)

    def test_empty_fingerprint_optional_returns_empty(self):
        """Empty fingerprint when not required returns empty set."""
        result = parse_fingerprints("", required=False)
        assert result == frozenset()

    def test_invalid_fingerprint_format_raises_error(self):
        """Invalid fingerprint format raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="Invalid GPG signer fingerprint"):
            parse_fingerprints("invalid-fingerprint", required=True)

    def test_none_value_optional_returns_empty(self):
        """None value when not required returns empty set."""
        result = parse_fingerprints(None, required=False)
        assert result == frozenset()


class TestLoadValues:
    """Test loading of configuration values."""

    def test_env_overrides_file(self, tmp_path: Path):
        """Environment variables override file values."""
        env_file = tmp_path / ".env"
        env_file.write_text("SEND_TELEGRAM=false\n")
        
        # Create temp env file
        with patch.dict(os.environ, {"SEND_TELEGRAM": "true"}):
            values = load_values(config_path=env_file)
            assert values.get("SEND_TELEGRAM") == "true"
        
        # Clean up
        if "SEND_TELEGRAM" in os.environ:
            del os.environ["SEND_TELEGRAM"]

    def test_file_values_loaded(self, tmp_path: Path):
        """Values from .env files are loaded."""
        env_file = tmp_path / ".env"
        env_file.write_text("CMD_TIMEOUT=60\n")
        
        values = load_values(config_path=env_file)
        assert values.get("CMD_TIMEOUT") == "60"

    def test_malformed_line_raises_error(self, tmp_path: Path):
        """Malformed configuration line raises ConfigurationError."""
        env_file = tmp_path / ".env"
        env_file.write_text("malformed-line-without-equals\n")
        
        with pytest.raises(ConfigurationError, match="Malformed configuration line"):
            load_values(config_path=env_file)

    def test_comments_are_ignored(self, tmp_path: Path):
        """Comments are ignored in configuration files."""
        env_file = tmp_path / ".env"
        env_file.write_text("# This is a comment\nCMD_TIMEOUT=60\n")
        
        values = load_values(config_path=env_file)
        assert values.get("CMD_TIMEOUT") == "60"
        assert "# This is a comment" not in values

    def test_secrets_not_logged(self, tmp_path: Path):
        """Secrets like bot tokens are not logged or exposed."""
        env_file = tmp_path / ".env"
        env_file.write_text("TG_BOT_TOKEN=secret_token_123\n")
        
        values = load_values(config_path=env_file)
        assert values.get("TG_BOT_TOKEN") == "secret_token_123"
        
        # Verify secret is in values (expected for internal use)
        # but should not appear in logs or JSON output
        # This is tested in workflow/tests rather than here


class TestRuntimeConfigConstruction:
    """Test construction of immutable RuntimeConfig."""

    def test_config_with_defaults(self, tmp_path: Path):
        """RuntimeConfig is built with default values."""
        values = {}
        config = build_runtime_config(values)
        
        assert config.paths.target_db == TARGET_DB_PATH
        assert config.policy.service_name == "x-ui"
        assert config.security.require_gpg_signature is True

    def test_config_with_cli_overrides(self, tmp_path: Path):
        """CLI overrides are applied to config."""
        values = {}
        cli_overrides = {"CMD_TIMEOUT": "60", "MAX_AGE_SECONDS": "7200"}
        
        config = build_runtime_config(values, cli_overrides=cli_overrides)
        
        assert config.policy.command_timeout == 60
        assert config.policy.max_age_seconds == 7200

    def test_config_with_force_and_dry_run(self, tmp_path: Path):
        """Force and dry_run options are properly set."""
        values = {}
        config = build_runtime_config(values, force=True, dry_run=True)
        
        assert config.options.force is True
        assert config.options.dry_run is True

    def test_config_with_notification_settings(self, tmp_path: Path):
        """Notification configuration is properly constructed."""
        values = {
            "SEND_TELEGRAM": "true",
            "TG_BOT_TOKEN": "test_token",
            "TG_CHAT_ID": "12345",
        }
        
        config = build_runtime_config(values)
        
        assert config.notifications.enabled is True
        assert config.notifications.bot_token == "test_token"
        assert config.notifications.chat_id == "12345"

    def test_config_with_gpg_requirements(self, tmp_path: Path):
        """GPG-related configuration is properly constructed."""
        values = {
            "REQUIRE_GPG_SIGNATURE": "true",
            "TRUSTED_GPG_SIGNER_FINGERPRINTS": "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234",
        }
        
        config = build_runtime_config(values)
        
        assert config.security.require_gpg_signature is True
        assert len(config.security.trusted_gpg_signer_fingerprints) == 1

    def test_config_with_unsafe_backup_path(self, tmp_path: Path):
        """Unsafe backup path option is properly set."""
        values = {}
        config = build_runtime_config(values, unsafe_backup_path=True)
        
        assert config.security.allow_unsafe_backup_path is True

    def test_missing_fingerprint_when_not_required(self, tmp_path: Path):
        """Missing fingerprints are allowed when not required."""
        values = {"REQUIRE_GPG_SIGNATURE": "false"}
        
        config = build_runtime_config(values)
        
        assert config.security.require_gpg_signature is False
        assert config.security.trusted_gpg_signer_fingerprints == frozenset()

    def test_invalid_fingerprint_raises_configuration_error(self, tmp_path: Path):
        """Invalid fingerprint format raises ConfigurationError."""
        values = {
            "REQUIRE_GPG_SIGNATURE": "true",
            "TRUSTED_GPG_SIGNER_FINGERPRINTS": "invalid",
        }
        
        with pytest.raises(ConfigurationError, match="Invalid GPG signer fingerprint"):
            build_runtime_config(values)

    def test_custom_reserved_ports_parsed(self, tmp_path: Path):
        """Custom reserved ports are properly parsed."""
        values = {"CUSTOM_RESERVED_PORTS": "22,80,443"}
        
        config = build_runtime_config(values)
        
        assert config.policy.custom_reserved_ports == frozenset({22, 80, 443})

    def test_ips_parsed(self, tmp_path: Path):
        """Primary and standby IPs are properly parsed."""
        values = {
            "PRIMARY_IP": "10.0.0.1",
            "STANDBY_IP": "10.0.0.2",
        }
        
        config = build_runtime_config(values)
        
        assert config.policy.primary_ip == "10.0.0.1"
        assert config.policy.standby_ip == "10.0.0.2"


class TestConfigurationValidation:
    """Test configuration validation edge cases."""

    def test_invalid_boolean_value_raises_error(self, tmp_path: Path):
        """Invalid boolean value raises ConfigurationError."""
        values = {"SEND_TELEGRAM": "maybe"}
        
        with pytest.raises(ConfigurationError, match="Invalid boolean value"):
            build_runtime_config(values)

    def test_invalid_timeout_raises_error(self, tmp_path: Path):
        """Invalid timeout raises ConfigurationError."""
        values = {"CMD_TIMEOUT": "-1"}
        
        with pytest.raises(ConfigurationError, match="must be positive"):
            build_runtime_config(values)

    def test_zero_timeout_raises_error(self, tmp_path: Path):
        """Zero timeout raises ConfigurationError."""
        values = {"CMD_TIMEOUT": "0"}
        
        with pytest.raises(ConfigurationError, match="must be positive"):
            build_runtime_config(values)

    def test_empty_string_overrides(self, tmp_path: Path):
        """Empty string values use defaults."""
        values = {"CMD_TIMEOUT": ""}
        
        config = build_runtime_config(values)
        # Should use default since empty string
        assert config.policy.command_timeout == 30

    def test_export_prefix_parsed(self, tmp_path: Path):
        """Export prefix in .env files is handled."""
        env_file = tmp_path / ".env"
        env_file.write_text("export CMD_TIMEOUT=60\n")
        
        values = load_values(config_path=env_file)
        assert values.get("CMD_TIMEOUT") == "60"

    def test_quoted_values_stripped(self, tmp_path: Path):
        """Quoted values have quotes stripped."""
        env_file = tmp_path / ".env"
        env_file.write_text('CMD_TIMEOUT="60"\n')
        
        values = load_values(config_path=env_file)
        assert values.get("CMD_TIMEOUT") == "60"

    def test_single_quoted_values_stripped(self, tmp_path: Path):
        """Single-quoted values have quotes stripped."""
        env_file = tmp_path / ".env"
        env_file.write_text("CMD_TIMEOUT='60'\n")
        
        values = load_values(config_path=env_file)
        assert values.get("CMD_TIMEOUT") == "60"