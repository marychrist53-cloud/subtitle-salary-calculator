# Project roadmap

This roadmap describes the next steps for the Subtitle Salary Calculator as both a small production tool and a portfolio project. Items marked **Complete** describe behavior already implemented in this repository; planned items are not promises or release dates.

## Project overview

The bot turns subtitle files submitted through Telegram into auditable monthly salary records. It supports three destinations with different accounting rules:

- **NAS** calculates line pay from `20ks` or `25ks` filename tags and adds the configured review fee for eligible part-one work.
- **PP** calculates line pay from `(v)`, `(p)`, `(mm)`, or `(old)` tags and never adds review fees.
- **LK** records filenames and line counts without calculating money.

The workflow has four main layers. `bot.py` handles Telegram commands and upload flow. `logic.py` decodes and validates subtitle formats, counts subtitle cues, checks language, and applies filename rules. `pending_store.py` and `storage.py` preserve local queue and settings state. `sheets.py` validates monthly records and writes them to Google Sheets atomically, with local snapshots for recovery and undo. `bridge.py` offers an authenticated path for n8n deployments.

The key reliability choice is to save an upload locally before attempting its Google Sheets write. A temporary Sheets failure therefore leaves work available for a later retry. Before changing a monthly tab, the bot validates existing rows and makes a local snapshot so a failed or unexpected update does not silently erase salary data.

## Current milestones

### 1. Accounting and file processing — Complete

- Separate NAS, PP, and LK destinations with per-chat month and destination selection.
- Count SRT cues, ASS dialogue events, and visible TXT lines. SRT cues with timestamps but no dialogue still count.
- Support UTF-8 and UTF-16 input and reject invalid files before calculating pay.
- Apply destination-specific rate tags and the NAS-only review fee rules.
- Sort LK filenames and mark matching `fix`/`check` names.

### 2. Recovery and safe sheet updates — Complete

- Save pending work before Google Sheets writes and retry after failures or restart.
- Validate sheet structure and totals before rewriting a monthly table.
- Snapshot old values before a write; support submitter-checked, confirmed undo.
- Avoid adding Telegram identity or record metadata to visible salary columns.

### 3. Commands, specifications, and offline CI — Complete

- Provide commands for destination, month, year, status, totals, links, queue recovery, reset, and undo.
- Document behavior in `spec.md` and setup in `README.md`.
- Run offline tests and lint checks in GitHub Actions for Windows and Linux.
- Ignore local configuration, service account keys, runtime records, queue data, backups, and caches.

## Recommended next milestones

### 4. Create a safe portfolio demo — High priority

- Add a small synthetic set of subtitle fixtures and expected totals that contains no real user files or salary records.
- Create a separate demo spreadsheet with fictional entries and the three destination layouts.
- Capture screenshots from that demo only; keep the operational Salary folder and its sheets private.
- Add a short demo walkthrough that shows `/sheet`, `/month`, a sample upload, `/status`, and `/payslit`.

### 5. Make deployment repeatable — High priority

- Add a deployment guide for one chosen host and a persistent data volume for the queue, metadata, and backups.
- Run the bot under a process supervisor that restarts it after a crash and prevents a second polling instance from starting.
- Add a startup health check for configuration, Google spreadsheet access, and writable local state without printing secrets.
- Document a tested backup and restore procedure for both local state and Google Sheets.

### 6. Improve operational visibility — Medium priority

- Use structured logs with a per-file correlation ID while omitting subtitle contents, bot tokens, service credentials, and personal identifiers.
- Add a compact health/status signal for operators, including pending queue size and last successful Sheets write.
- Make retry outcomes clearer when a file is permanently invalid versus temporarily blocked by a service outage.

### 7. Expand compatibility checks — Medium priority

- Add fixture-based coverage for SRT variants seen in real subtitle tools, including optional cue IDs, compound IDs, timestamp precision, and inconsistent separators.
- Run an opt-in integration check against dedicated Telegram and Google test resources; keep it isolated from live salary sheets.
- Add migration examples for older sheet layouts and recovery cases before changing the sheet schema.

### 8. Prepare for broader reuse — Later

- Move rate rules, review fees, and destination definitions into validated configuration if other teams need different policies.
- Add an operator guide for onboarding, monthly closeout, and audit/reconciliation.
- Choose and add a license only after deciding whether others may reuse, modify, and redistribute the code.

## Portfolio presentation checklist

- Explain the accounting rules and the reason for separate destination tables.
- Show how local queue persistence and atomic sheet updates handle failures.
- Include the architecture diagram and the offline CI results.
- Use synthetic names, synthetic amounts, and a demo spreadsheet; never publish live salary sheets, credentials, Telegram IDs, or subtitle content.
- State current limitations plainly: Google Sheets and Telegram are external dependencies, deployment is operator-managed, and the bot is intended to run as a single instance per token/data directory.
