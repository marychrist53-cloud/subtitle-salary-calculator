import importlib
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not call external services")
    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    monkeypatch.setattr("httpx.AsyncClient.send", blocked)


@pytest.fixture
def bot_module(tmp_path, monkeypatch):
    with patch("dotenv.load_dotenv", return_value=False):
        module = importlib.import_module("bot")
    monkeypatch.setattr(module, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(module, "PENDING_FILES_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(module, "schedule_batch_summary", lambda *args: None)
    return module
