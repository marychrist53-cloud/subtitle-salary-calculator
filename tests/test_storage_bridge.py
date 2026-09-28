import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bridge import make_bridge
from storage import read_json, write_json


def test_failed_atomic_save_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write_json(path, {"existing": True})
    def fail(*args):
        raise OSError("Disk error")
    monkeypatch.setattr("storage.os.replace", fail)
    with pytest.raises(OSError):
        write_json(path, {"replacement": True})
    assert read_json(path) == {"existing": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_bridge_authentication_and_update_validation():
    async def check():
        app = SimpleNamespace(bot=None, update_queue=asyncio.Queue())
        secret = "a-long-test-secret"
        async with TestClient(TestServer(make_bridge(app, secret))) as client:
            response = await client.post("/" + secret, json={"update_id": 1})
            assert response.status == 403
            headers = {"X-Telegram-Bot-Api-Secret-Token": secret}
            response = await client.post("/" + secret, json={"no_update_id": 1}, headers=headers)
            assert response.status == 400
            response = await client.post("/" + secret, json={"update_id": 1}, headers=headers)
            assert response.status == 200
            assert (await app.update_queue.get()).update_id == 1
    asyncio.run(check())
