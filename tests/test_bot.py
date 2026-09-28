import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pending_store import load_pending_files, save_pending_files


TEXT = "1\n00:00:01,000 --> 00:00:02,000\nမြန်မာ"


def context():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), chat_data={}, user_data={},
                           args=[], job_queue=SimpleNamespace(run_once=Mock()))


def queued(name):
    return {"file_name": name, "text": TEXT, "user_id": "7", "chat_id": "8"}


def settings(bot):
    bot.save_setting(8, "sheet", "NAS")
    bot.save_setting(8, "month", "9.2026")


def test_failure_does_not_block_other_files_and_retry_keeps_target(bot_module, monkeypatch):
    bot = bot_module
    settings(bot)
    ctx = context()
    save_pending_files(bot.PENDING_FILES_FILE, 8, [queued("Fail20ks.srt"), queued("OK20ks.srt")])
    record = Mock(side_effect=[RuntimeError("offline"), {}])
    monkeypatch.setattr(bot.sheets, "record", record)
    asyncio.run(bot.continue_pending(ctx, 8))
    pending = load_pending_files(bot.PENDING_FILES_FILE, 8)
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["month"] == "9.2026"
    bot.save_setting(8, "month", "10.2026")
    bot.save_setting(8, "sheet", "PP")
    record.side_effect = None
    record.return_value = {"recovered": True}
    asyncio.run(bot.continue_pending(ctx, 8))
    assert record.call_args.args[0]["source"] == "NAS"
    assert record.call_args.args[1] == "9.2026"
    assert load_pending_files(bot.PENDING_FILES_FILE, 8) == []


def test_startup_survives_service_failure(bot_module, monkeypatch):
    bot = bot_module
    settings(bot)
    save_pending_files(bot.PENDING_FILES_FILE, 8, [queued("Fail20ks.srt")])
    monkeypatch.setattr(bot.sheets, "record", Mock(side_effect=RuntimeError("offline")))
    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=RuntimeError()),
                                               send_message=AsyncMock()),
                          job_queue=SimpleNamespace(run_once=Mock()), chat_data=defaultdict(dict))
    asyncio.run(bot.post_init(app))
    assert len(load_pending_files(bot.PENDING_FILES_FILE, 8)) == 1


def test_upload_is_saved_before_write_with_submitter_and_time(bot_module, monkeypatch):
    bot = bot_module
    settings(bot)
    ctx = context()
    ctx.bot.get_file = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=TEXT.encode("utf-16"))))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7, full_name="Test User"))
    message = SimpleNamespace(message_id=99, date=datetime(2026, 9, 25, tzinfo=timezone.utc),
                              chat=SimpleNamespace(type="private"))
    document = SimpleNamespace(file_id="file", file_size=100)

    def write(entry, month):
        saved = load_pending_files(bot.PENDING_FILES_FILE, 8)
        assert saved[0]["record_id"] == "8:99"
        assert entry["submitted_by"] == "Test User"
        assert entry["submitted_at"] == "2026-09-25T00:00:00+00:00"
        raise RuntimeError("offline")

    monkeypatch.setattr(bot.sheets, "record", write)
    asyncio.run(bot._process_document(update, ctx, message, 8, document, "Movie20ks.srt"))
    assert len(load_pending_files(bot.PENDING_FILES_FILE, 8)) == 1


def test_unsupported_file_is_not_downloaded(bot_module):
    ctx = context()
    ctx.bot.get_file = AsyncMock()
    asyncio.run(bot_module._process_document(None, ctx, None, 8, None, "Movie20ks.pdf"))
    ctx.bot.get_file.assert_not_awaited()
    assert ctx.chat_data["batch"]["errors"] == 1


def test_undo_requires_preview_and_exact_chat_confirmation(bot_module, monkeypatch):
    bot = bot_module
    settings(bot)
    ctx = context()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=8), effective_user=SimpleNamespace(id=7),
                             message=SimpleNamespace(reply_text=AsyncMock()))
    monkeypatch.setattr(bot.sheets, "latest_record", Mock(return_value={"record_id": "id", "file_name": "Movie",
                                                                      "total": 20, "review": 1500}))
    undo = Mock(return_value=True)
    monkeypatch.setattr(bot.sheets, "undo_record", undo)
    ctx.args = ["confirm"]
    asyncio.run(bot.undo_command(update, ctx))
    undo.assert_not_called()
    ctx.args = []
    asyncio.run(bot.undo_command(update, ctx))
    ctx.args = ["confirm"]
    update.effective_chat.id = 9
    asyncio.run(bot.undo_command(update, ctx))
    undo.assert_not_called()
    update.effective_chat.id = 8
    asyncio.run(bot.undo_command(update, ctx))
    undo.assert_called_once_with("NAS", "9.2026", "id", 7, 8)


def test_status_reports_current_and_unset_settings(bot_module):
    bot = bot_module
    ctx = context()
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=8), effective_message=message)
    asyncio.run(bot.status_command(update, ctx))
    assert "Sheet: not set" in message.reply_text.await_args.args[0]
    bot.save_setting(8, "sheet", "PP")
    bot.save_setting(8, "month", "9.2026")
    asyncio.run(bot.status_command(update, ctx))
    assert "Sheet: PP" in message.reply_text.await_args.args[0]
    assert "Month: 9.2026" in message.reply_text.await_args.args[0]


def test_payslit_requires_settings_and_reports_selected_month(bot_module, monkeypatch):
    bot = bot_module
    ctx = context()
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=8), effective_message=message)

    asyncio.run(bot.payslit_command(update, ctx))
    assert "/sheet" in message.reply_text.await_args.args[0]

    settings(bot)
    monkeypatch.setattr(bot.sheets, "month_summary", Mock(return_value={
        "overall_total": 1600,
        "review_count": 1,
        "review_total": 1500,
        "line_count": 5,
        "file_count": 2,
    }))
    asyncio.run(bot.payslit_command(update, ctx))
    bot.sheets.month_summary.assert_called_once_with("NAS", "9.2026")
    output = message.reply_text.await_args.args[0]
    assert "Overall total: 1,600 Ks" in output
    assert "Review files: 1" in output
    assert "Review total: 1,500 Ks" in output
    assert "Total lines: 5" in output
    assert "File count: 2" in output


def test_bot_menu_lists_every_command_handler(bot_module):
    commands = {command.command for command in bot_module.BOT_COMMANDS}
    assert commands == {
        "start", "help", "sheet", "month", "status", "payslit", "link",
        "reset", "resetmonth", "resetyear", "pending", "retry", "undo",
    }
    assert "confirm" in next(command.description for command in bot_module.BOT_COMMANDS
                              if command.command == "undo")


def test_untagged_upload_waits_for_lk_choice_and_records_counts(bot_module, monkeypatch):
    bot = bot_module
    ctx = context()
    ctx.bot.get_file = AsyncMock(return_value=SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=TEXT.encode("utf-8"))))
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=8),
                             effective_user=SimpleNamespace(id=7, full_name="Test User"))
    message = SimpleNamespace(message_id=101, date=datetime(2026, 9, 26, tzinfo=timezone.utc),
                              chat=SimpleNamespace(type="private"))
    document = SimpleNamespace(file_id="file", file_size=100)
    record = Mock(return_value={})
    monkeypatch.setattr(bot.sheets, "record", record)
    asyncio.run(bot._process_document(update, ctx, message, 8, document, "Show checked.srt"))
    record.assert_not_called()
    assert len(load_pending_files(bot.PENDING_FILES_FILE, 8)) == 1
    asyncio.run(bot.sheet_chosen(update, ctx, "LK"))
    record.assert_not_called()
    asyncio.run(bot.month_chosen(update, ctx, "9.2026"))
    entry = record.call_args.args[0]
    assert entry["source"] == "LK" and entry["lines"] == 1 and entry["total_fee"] == 0
    assert load_pending_files(bot.PENDING_FILES_FILE, 8) == []
    reply = ctx.bot.send_message.await_args.args[1]
    assert "Lines: 1" in reply and "LK" in reply and "Ks" not in reply


def test_lk_payslit_has_only_counts(bot_module, monkeypatch):
    bot = bot_module
    bot.save_setting(8, "sheet", "LK")
    bot.save_setting(8, "month", "9.2026")
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=8), effective_message=message)
    monkeypatch.setattr(bot.sheets, "month_summary", Mock(return_value={"line_count": 200, "file_count": 1}))
    asyncio.run(bot.payslit_command(update, context()))
    output = message.reply_text.await_args.args[0]
    assert "Total lines: 200" in output and "File count: 1" in output
    assert "Ks" not in output and "Review" not in output


def test_handlers_are_registered_without_chat_id_guard(bot_module, monkeypatch):
    bot = bot_module
    builder = Mock()
    app = builder.token.return_value.post_init.return_value.concurrent_updates.return_value.build.return_value
    monkeypatch.setattr(bot.Application, "builder", lambda: builder)
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(bot, "BOT_MODE", "poll")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "123")
    bot.main()
    callbacks = {call.args[0].callback for call in app.add_handler.call_args_list}
    assert {bot.handle_document, bot.button_handler, bot.start_command, bot.payslit_command} <= callbacks
    assert not hasattr(bot, "ALLOWED_CHAT_IDS")
