"""Shared test setup: tests never read the real macOS Keychain (the Keychain tests re-enable it against a fake)."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    monkeypatch.setenv("ICLOUD_KEYCHAIN", "false")
