# Contributing

Use Python 3.10+ and install `requirements-dev.txt` in a virtual environment. Run `python -m pytest` and `python -m ruff check .` before proposing a change. CI checks Windows and Linux.

Keep changes to rates and review eligibility covered by examples. Preserve the rule that both an explicit part-one marker and a first-episode marker are needed for a series review fee, regardless of season.

Changes to persistence must preserve legacy worksheet columns and avoid deleting unknown data. Test failures before and after a Google write, retry deduplication, ownership checks for undo, and recovery after restart. Keep tests offline and use temporary directories for local state.

Never include credentials, chat settings, subtitle contents, live spreadsheet IDs, or backups in issues and commits. Run `python scripts/check_secrets.py` to check prospective and staged files. Update `spec.md` and the README whenever behavior or configuration changes.
