"""Tests for stable Qt application-data paths."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QStandardPaths

from mailbrief import __version__
from mailbrief.errors import ConfigurationError
from mailbrief.paths import AppPaths, configure_qt_identity


def test_configure_qt_identity_uses_package_version() -> None:
    configure_qt_identity()
    assert QCoreApplication.applicationName() == "MailBrief"
    assert QCoreApplication.applicationVersion() == __version__


def test_paths_create_stable_children(tmp_path: Path) -> None:
    root = tmp_path / "local-data"
    with patch.object(QStandardPaths, "writableLocation", return_value=str(root)):
        paths = AppPaths.from_qt()
    assert paths.data_dir == root.resolve()
    assert paths.database_path == root.resolve() / "mailbrief.sqlite3"
    assert paths.microsoft_token_cache_path == root.resolve() / "msal-token-cache.bin"
    assert root.is_dir()
    with pytest.raises(FrozenInstanceError):
        paths.data_dir = tmp_path  # type: ignore[misc]


def test_empty_qt_location_is_configuration_error() -> None:
    with (
        patch.object(QStandardPaths, "writableLocation", return_value=""),
        pytest.raises(ConfigurationError, match="resolve"),
    ):
        AppPaths.from_qt()
