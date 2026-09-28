# Salary calculator bot

The complete setup, command reference, recovery guide, and development instructions are in the [project README](../README.md). The behavior contract is [spec.md](../spec.md).

From this directory:

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Fill in the token, Google key path, and spreadsheet IDs.
python bot.py
```

Use `BOT_MODE=poll` for polling, `webhook` for a direct Telegram webhook, or `bridge` when n8n receives and forwards updates. Run only one instance against the same token and spreadsheets.
