"""
Tests for config.py's Settings - env var parsing, validation, and the
computed properties other modules rely on (cors_origins_list, etc).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from pydantic import ValidationError

from config import Settings


class TestDefaults:
    def test_defaults_are_sane_for_local_dev(self):
        s = Settings(_env_file=None)
        assert s.ENVIRONMENT == "development"
        assert s.PORT == 8000
        assert s.is_production is False

    def test_no_r2_creds_means_not_configured(self):
        s = Settings(_env_file=None)
        assert s.r2_configured is False


class TestCorsOriginsList:
    def test_wildcard_stays_wildcard(self):
        s = Settings(_env_file=None, CORS_ORIGINS="*")
        assert s.cors_origins_list == ["*"]

    def test_comma_separated_origins_are_split_and_stripped(self):
        s = Settings(_env_file=None, CORS_ORIGINS="https://a.com, https://b.com ,https://c.com")
        assert s.cors_origins_list == ["https://a.com", "https://b.com", "https://c.com"]

    def test_empty_string_yields_empty_list_not_wildcard(self):
        s = Settings(_env_file=None, CORS_ORIGINS="")
        assert s.cors_origins_list == []


class TestValidation:
    def test_invalid_log_level_raises(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, LOG_LEVEL="NOT_A_LEVEL")

    def test_log_level_is_case_insensitive(self):
        s = Settings(_env_file=None, LOG_LEVEL="debug")
        assert s.LOG_LEVEL == "DEBUG"

    def test_invalid_environment_raises(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, ENVIRONMENT="not-a-real-env")


class TestR2Configured:
    def test_partial_r2_creds_do_not_count_as_configured(self):
        s = Settings(_env_file=None, R2_ACCOUNT_ID="abc", R2_ACCESS_KEY_ID="def")
        assert s.r2_configured is False

    def test_all_three_required_r2_creds_count_as_configured(self):
        s = Settings(
            _env_file=None,
            R2_ACCOUNT_ID="abc",
            R2_ACCESS_KEY_ID="def",
            R2_SECRET_ACCESS_KEY="ghi",
        )
        assert s.r2_configured is True
