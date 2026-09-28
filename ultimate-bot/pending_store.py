"""Atomic JSON persistence for files waiting on per-chat sheet/month choices."""

from pathlib import Path
from storage import read_json, write_json


def _read_store(path: Path) -> dict:
    return read_json(path)


def pending_chat_ids(path: Path) -> list[int]:
    return [int(chat_id) for chat_id, items in _read_store(path).items() if items]


def load_pending_files(path: Path, chat_id: int) -> list[dict]:
    files = _read_store(path).get(str(chat_id), [])
    if not isinstance(files, list):
        raise ValueError(f"Pending file queue for chat {chat_id} must be a JSON array")
    return files


def save_pending_files(path: Path, chat_id: int, files: list[dict]) -> None:
    store = _read_store(path)
    key = str(chat_id)
    if files:
        store[key] = files
    else:
        store.pop(key, None)

    write_json(path, store)
