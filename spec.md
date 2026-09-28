# Salary Calculator specification

## Purpose and components

Calculate Burmese subtitle translation pay from Telegram documents and record it in Google Sheets. This repository contains only the salary calculator.

- `ultimate-bot/logic.py`: decoding, subtitle validation, language and filename analysis.
- `ultimate-bot/bot.py`: Telegram commands, submission metadata, persistent queuing and recovery.
- `ultimate-bot/sheets.py`: strict parsing of monthly tables, atomic writes, backups and undo.
- `ultimate-bot/storage.py` and `pending_store.py`: atomic JSON storage.
- `ultimate-bot/bridge.py`: authenticated forwarding endpoint for n8n.

## Accepted files

Accept `.srt`, `.ass`, and `.txt`, case-insensitively. Reject other extensions before download. Apply configured upload and queue limits. Decode UTF-8 (with optional BOM), UTF-16 with BOM, or BOM-less UTF-16 when it can be recognized as Burmese subtitle text. Retain Windows-1252/Latin-1 fallback for legacy text. Reject binary control characters.

SRT cues require valid increasing timestamps. Cue identifiers are optional; compound numeric identifiers such as `34-1` are accepted. The parser also accepts consecutive cues without a blank separator. Timestamps may use comma or period fractions with one to three digits and one or more hour digits. A cue with blank subtitle text still counts as one line; blank separator lines between cues do not count. A malformed timing line or inconsistent cue block rejects the entire file rather than silently undercounting it. ASS requires non-empty Dialogue events matching the declared event Format (standard ten fields when absent); Text must be last. TXT counts visible non-empty lines. Empty or malformed content earns no base or review fee.

Language checks use visible subtitle dialogue, excluding ASS metadata and formatting tags. At least one Myanmar character is required. Rejected untranslated documents are not logged; deletion is attempted in non-private chats when permitted.

## Rates and review fee

| Destination | Tag | Ks per line |
| --- | --- | ---: |
| NAS | `20ks` | 20 |
| NAS | `25ks` | 25 |
| PP | `(v)` | 20 |
| PP | `(p)` | 18 |
| PP | `(mm)` | 22 |
| PP | `(old)` | 15 |

Filename matching is case-insensitive. `20ks` and `25ks` files belong to NAS; parenthesized rate tags belong to PP. If the chat's selected sheet does not match the filename tag, reject the file and tell the user which sheet to choose. The `25ks` tag takes precedence over `20ks` if both appear, and a larger number such as `120ks` must not be mistaken for `20ks`.

Base pay = valid cue/line count × rate. NAS adds 1,500 Ks for an explicit `p1`/`p 1`/`part1`/`part 1` marker on a movie, or on episode one of a series in any season. Episode forms include `e1`, `e01`, `ep1`, `ep01`, `episode 1`, `S02E01`, and `2-1`, with spaces before episode numbers allowed. Episode one without a part-one marker does not earn review pay. Later episodes and later parts do not qualify. PP never adds review pay.

## LK count-only destination

LK uses the same per-chat sheet selection and monthly tabs (`M.YYYY`) as NAS and PP, in the Salary LK spreadsheet (`LK_SHEET_ID`). It applies the same subtitle counting and language validation, including counting empty SRT cues. Rate tags are optional and ignored for LK; no base or review fees are calculated.

Each LK month contains only File Name and Line Count columns, plus total line and file counts. Sort filenames alphabetically with case-insensitive comparison on every write, including undo. Reject the same exact filename within the selected LK month, using the existing trimmed, case-sensitive duplicate rule. Highlight filename cells yellow when their names contain `fix`, `fixed`, `check`, or `checked`, ignoring case; this matching is substring-based.

LK works with status, year changes, reset, links, queue recovery, and undo. `/payslit` and upload replies show counts without money fields for LK. Untagged valid uploads received before a destination is chosen remain queued until the user chooses a sheet; NAS/PP then require their usual tags.

## Destination and durable queue

Sheet/month choices are shared per chat and persisted locally. Month inputs `M.YYYY`, `M/YYYY`, and `M-YYYY` normalize to `M.YYYY`, with valid month and year required.

Every valid analyzed upload is atomically saved before its Google write. Each queued item contains subtitle text, filename, stable record ID, original Telegram message ID, submitting user/chat, display name, and original submission time in UTC. Legacy queue items without identity remain processable without inventing a user identity.

A queue item acquires its destination once both choices are available. This target remains fixed through retries and subsequent changes to chat settings. Attempt each file once per pass; retain failures, continue other files, and remove completed/rejected/duplicate items. Startup attempts recovery without stopping the bot on Google failures. `/retry` performs another pass; no continuous retry loop runs in the background.

## Sheets, history, and corrections

NAS and PP each have one worksheet per month and tables only for their assigned rates. NAS has File Name, Line Count, Total (base Ks), and Review (Ks); PP omits Review. Do not add submitter, timestamp, Telegram identity, or record ID columns to the visible tables. Keep ownership metadata in the ignored local `record_metadata.json` file so `/undo` can verify submitter and chat. When reading old tables with those columns, migrate their metadata locally and omit those columns on the next table rewrite; do not invent missing historical identities.

Preserve existing records while recalculating per-rate and overall totals. Refuse unexpected rows, columns, rates, invalid numeric data, and base fees inconsistent with lines × rate. Monthly tabs are dedicated to this application and should not be edited concurrently with bot writes.

Before every record or undo write, save the old values and sheet identifiers to an ignored local backup file. Backup failure prevents the Google write. Update size and cells using one Google Sheets batchUpdate request; do not clear first. Cell strings must be written literally, never interpreted as formulas. A rejected atomic request leaves old data intact. Local record metadata reconciles a write whose response was lost; duplicate filenames are skipped within the destination/month.

`/undo` previews the original user's latest recorded submission in the selected destination/month and current chat. `/undo confirm` within two minutes removes that exact record, verifies ownership again, and recalculates totals with a backup. Historical rows without ownership metadata cannot be undone through the bot. A saved retry for the same record must be reconciled first. Confirmation state is not persisted across restart.

## Commands and operation

- `/start`, `/help`: instructions; `/start` includes current chat settings.
- `/sheet`, `/month`: set destination choices.
- `/status`: show the current chat's selected sheet and month, or say either is not set.
- `/payslit`: summarize the selected month with overall salary including review fees, number of files receiving review fees, review total, total lines, and file count. PP review values are zero.
- `/reset`: clear choices while retaining queued files and their assigned targets.
- `/resetmonth`: legacy alias for `/reset`.
- `/resetyear`: choose a year for the selected month while keeping the month number and sheet unchanged.
- `/link`: spreadsheet links.
- `/pending`: saved queue count and a bounded filename listing.
- `/retry`: attempt saved work.
- `/undo`, `/undo confirm`: preview and confirm removal.

Reply per attempt and send a batch summary after the configured quiet interval. Failed sends are retried once; delivery cannot be guaranteed during Telegram outages. Every Telegram account can use commands, upload files, and use callbacks; no chat ID allowlist is applied. Group members share settings; manual month prompts are scoped to their chat and requesting user.

`BOT_MODE=poll` polls Telegram. `webhook` registers a direct public webhook. `bridge` receives authenticated n8n updates without changing Telegram's webhook registration. Direct and bridge modes require a secret; bridge accepts only bounded JSON Telegram updates. The deployment must run one bot instance, with persistent local storage and private access to keys, queues, and backups.

## Repository and verification

Python 3.10+. Runtime dependencies are in `ultimate-bot/requirements.txt`; development dependencies are in `requirements-dev.txt`. Root README documents setup, modes and recovery. Environment examples contain no credentials. Git ignores secrets, private state, backups, caches and virtual environments. A pre-push scanner checks Git candidate and staged files for common credentials and private filenames.

Run `python -m pytest`, `python -m ruff check .`, and `python scripts/check_secrets.py`. Offline tests exercise salary rules, malformed files, encodings, legacy/new sheet layouts, atomic update failures, retry recovery, submitter ownership and bridge authentication. GitHub Actions runs the checks on Windows and Linux with Python 3.10 and 3.12. Live Telegram/Google behavior needs an operator's integration check against a dedicated test bot and test spreadsheets before production rollout. See `ROADMAP.md` for the portfolio and operational plan.
