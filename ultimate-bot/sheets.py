"""Google Sheets bookkeeping for the ultimate salary calculator.

Creates/uses "Salary NAS", "Salary PP", and "Salary LK" spreadsheets and keeps
one worksheet (tab) per month, named like "8.2026".

Each monthly tab holds tables for the rates assigned to that destination. NAS
uses 20ks and 25ks; PP uses (v), (p), (mm), and (old). NAS tables carry an
extra Review column. Totals sit under each table and an overall grand total
sits at the bottom of the tab.

The tab is rebuilt from its parsed contents on every write, so the spreadsheet
itself stays the single source of truth.
LK records filenames and line counts, sorted alphabetically, without fees.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import gspread
from logic import detect_rate_tag
from storage import read_json, write_json

logger = logging.getLogger(__name__)

MONTH_TITLE_RE = re.compile(r"^\d{1,2}\.\d{4}$")
RATE_ROW_RE = re.compile(r"^Rate:\s*(\d+)\s+Ks per line")
FOLDER_URL_RE = re.compile(r"folders/([a-zA-Z0-9_-]+)")


def _with_retry(func, *args, **kwargs):
    """Run a Sheets API write, retrying on 429 (per-minute quota) with backoff."""
    delays = (2, 5, 10)
    for attempt, delay in enumerate((0,) + delays):
        if delay:
            logger.warning("Sheets write rate-limited, retrying in %ss (%d/%d)", delay, attempt, len(delays))
            time.sleep(delay)
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as exc:
            if "429" not in str(exc) or attempt == len(delays):
                raise

TOTAL_LABELS = {
    "File count",
    "Total line count",
    "Review count",
    "Total review fee (Ks)",
    "Grand total fee (Ks)",
    "Total fee (Ks)",
}

# Order of tables inside a tab, and their column layout.
SOURCE_RATES = {
    # Rate tag and destination are paired; do not mix the two tag families.
    "NAS": [
        (20, "20ks"),
        (25, "25ks"),
    ],
    "PP": [
        (20, "(v)"),
        (18, "(p)"),
        (22, "(mm)"),
        (15, "(old)"),
    ],
    "LK": [(0, "")],  # Internal group for count-only records; never displayed as a rate.
}
ALL_RATES = {15, 18, 20, 22, 25}
SOURCE_COLUMNS = {
    "NAS": ["File Name", "Line Count", "Total (Ks)", "Review (Ks)"],
    "PP": ["File Name", "Line Count", "Total (Ks)"],
    "LK": ["File Name", "Line Count"],
}
LK_HIGHLIGHT_PATTERN = re.compile(r"fix(?:ed)?|check(?:ed)?", re.IGNORECASE)
LEGACY_AUDIT_COLUMNS = {
    "submitted_by": "Submitted By",
    "submitted_at": "Submitted At (UTC)",
    "user_id": "Telegram User ID",
    "chat_id": "Telegram Chat ID",
    "record_id": "Record ID",
}


def normalize_month(raw: str) -> str | None:
    """Accept "8.2026", "8/2026", "08-2026" and return canonical "8.2026"."""
    raw = raw.strip()
    m = re.match(r"^(\d{1,2})[.\-/](\d{4})$", raw)
    if not m:
        return None
    month, year = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12 or not 1 <= year <= 9999:
        return None
    return f"{month}.{year}"


def drive_folder_id() -> str | None:
    """Resolve GOOGLE_DRIVE_FOLDER_ID — accepts a raw ID or a full folder URL."""
    raw = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not raw:
        return None
    m = FOLDER_URL_RE.search(raw)
    return m.group(1) if m else raw


# Optional: open existing (user-created) spreadsheets by ID instead of the bot
# creating its own. Needed when the service account can't create files
# (known Google limitation: "Drive storage quota has been exceeded" on fresh
# service accounts).
ENV_SHEET_KEYS = {source: f"{source}_SHEET_ID" for source in SOURCE_RATES}


def env_sheet_id(source: str) -> str | None:
    return os.environ.get(ENV_SHEET_KEYS[source], "").strip() or None


def find_duplicate(entries_by_rate: dict[int, list[dict]], file_name: str) -> bool:
    """True if the exact file name is already logged in any rate table."""
    target = file_name.strip()
    return any(
        e["file_name"].strip() == target
        for entries in entries_by_rate.values()
        for e in entries
    )


class SalarySheets:
    def __init__(self, service_account_path: str, ids_file: str = "sheet_ids.json", backup_dir=None,
                 metadata_file=None):
        self.service_account_path = service_account_path
        self.ids_file = Path(ids_file)
        self.backup_dir = Path(backup_dir) if backup_dir else self.ids_file.parent / "backups"
        self.metadata_file = Path(metadata_file) if metadata_file else self.ids_file.parent / "record_metadata.json"
        self._client = None
        self._docs: dict[str, gspread.Spreadsheet] = {}

    # ── Setup ──
    @property
    def client(self) -> gspread.Client:
        if self._client is None:
            self._client = gspread.service_account(filename=self.service_account_path)
        return self._client

    def _load_ids(self) -> dict:
        return read_json(self.ids_file)

    def _save_ids(self, ids: dict):
        write_json(self.ids_file, ids)

    def get_document(self, source: str) -> gspread.Spreadsheet:
        """Return the spreadsheet for NAS, PP, or LK.

        Priority: <SOURCE>_SHEET_ID from the environment (a sheet you
        created and shared with the service account) → sheet_ids.json (a sheet
        this bot created earlier) → create a new one.
        """
        if source in self._docs:
            return self._docs[source]

        from_env = env_sheet_id(source)
        ids = self._load_ids()
        sheet_id = from_env or ids.get(source)

        if sheet_id:
            try:
                doc = self.client.open_by_key(sheet_id)
                if from_env:
                    ids[source] = from_env
                    self._save_ids(ids)
                self._docs[source] = doc
                return doc
            except gspread.SpreadsheetNotFound:
                if from_env:
                    raise RuntimeError(
                        f"{ENV_SHEET_KEYS[source]} points to a spreadsheet that "
                        f"doesn't exist or isn't shared with the service account "
                        f"(share it as Editor with the service-account email)."
                    )
                logger.warning("%s spreadsheet id %s not found, recreating", source, sheet_id)
            except gspread.exceptions.APIError as exc:
                # e.g. 403 when the sheet exists but isn't shared with the SA
                raise RuntimeError(
                    f"The service account can't open the {source} spreadsheet — "
                    f"share it as Editor with the service-account email "
                    f"({exc})"
                ) from exc

        folder_id = drive_folder_id()
        if folder_id:
            # Created inside the user's shared folder, so it's visible to them.
            doc = self.client.create(f"Salary {source}", folder_id=folder_id)
        else:
            doc = self.client.create(f"Salary {source}")
            logger.warning(
                "GOOGLE_DRIVE_FOLDER_ID is not set — 'Salary %s' was created in the "
                "service account's own Drive root. Share that spreadsheet with your "
                "Google account, or set the folder ID so future files are created "
                "in your folder.",
                source,
            )
        ids[source] = doc.id
        self._save_ids(ids)
        self._docs[source] = doc
        logger.info("Created spreadsheet 'Salary %s' (id=%s)", source, doc.id)
        return doc

    def get_month_tab(self, source: str, month: str) -> gspread.Worksheet:
        doc = self.get_document(source)
        try:
            return doc.worksheet(month)
        except gspread.WorksheetNotFound:
            ws = doc.add_worksheet(month, rows=100, cols=8)
            logger.info("Created %s tab %s", source, month)
            return ws

    # ── Parsing ──
    def _parse_tab(self, values: list[list[str]]) -> dict:
        """Read legacy/new tables; refuse unexpected rows instead of discarding them."""
        entries = {source: {} for source in SOURCE_RATES}
        source = None
        rate = None
        columns = []
        collecting = False
        for number, row in enumerate(values, 1):
            row = [str(value) for value in row]
            first = row[0].strip() if row else ""
            if not first and not any(value.strip() for value in row):
                continue
            title = re.fullmatch(r"(NAS|PP|LK) SALARY — (\d{1,2}\.\d{4})", first)
            if title:
                source, rate, collecting = title[1], None, False
                if source == "LK":
                    rate = 0
                continue
            match = RATE_ROW_RE.fullmatch(first.split(" (tag:", 1)[0])
            if match and source:
                if source == "LK":
                    raise ValueError(f"LK cannot contain rate tables (row {number}).")
                rate, collecting = int(match[1]), False
                if rate not in ALL_RATES:
                    raise ValueError(f"Unsupported rate in worksheet row {number}; repair it before retrying.")
                continue
            if first == "File Name" and source and rate is not None:
                columns = row[:]
                while columns and not columns[-1]:
                    columns.pop()
                base = SOURCE_COLUMNS[source]
                if columns not in (base, base + list(LEGACY_AUDIT_COLUMNS.values())):
                    raise ValueError(f"Unrecognized worksheet columns in row {number}.")
                collecting = True
                continue
            if first in TOTAL_LABELS or first == "OVERALL GRAND TOTAL (all rates) (Ks)":
                collecting = False
                continue
            if not (collecting and source and rate is not None and first):
                raise ValueError(f"Unrecognized worksheet row {number}; existing data was not replaced.")
            if any(value.strip() for value in row[len(columns):]):
                raise ValueError(f"Unexpected data after the table columns in row {number}.")

            def integer(index, optional=False):
                value = row[index].strip().replace(",", "") if len(row) > index else ""
                if not value and optional:
                    return 0
                if not re.fullmatch(r"\d+", value):
                    raise ValueError(f"Invalid amount/count in worksheet row {number}.")
                return int(value)

            entry = {"file_name": first, "lines": integer(1), "total": 0 if source == "LK" else integer(2),
                     "review": integer(3, optional=True) if source == "NAS" else 0}
            if entry["total"] != entry["lines"] * rate:
                raise ValueError(f"Base fee does not match lines × rate in row {number}.")
            for key, label in LEGACY_AUDIT_COLUMNS.items():
                if label in columns:
                    index = columns.index(label)
                    if index < len(row) and row[index]:
                        entry[key] = row[index]
            entries[source].setdefault(rate, []).append(entry)
        return entries

    @staticmethod
    def _metadata_key(source, month, record_id):
        return f"{source}|{month}|{record_id}"

    def _load_metadata(self):
        return read_json(self.metadata_file)

    def _store_entry_metadata(self, source, month, entry, status):
        record_id = str(entry.get("record_id") or "")
        if not record_id:
            return
        metadata = self._load_metadata()
        metadata[self._metadata_key(source, month, record_id)] = {
            "source": source,
            "month": month,
            "record_id": record_id,
            "file_name": entry["file_name"],
            "submitted_by": entry.get("submitted_by", ""),
            "submitted_at": entry.get("submitted_at", ""),
            "user_id": str(entry.get("user_id", "")),
            "chat_id": str(entry.get("chat_id", "")),
            "total": entry["base_fee"],
            "review": entry.get("review_fee", 0),
            "status": status,
        }
        write_json(self.metadata_file, metadata)

    def _migrate_legacy_metadata(self, source, month, entries):
        metadata = self._load_metadata()
        changed = False
        for rows in entries.values():
            for row in rows:
                record_id = str(row.get("record_id") or "")
                if not record_id:
                    continue
                key = self._metadata_key(source, month, record_id)
                current = metadata.get(key, {})
                migrated = {
                    "source": source,
                    "month": month,
                    "record_id": record_id,
                    "file_name": row["file_name"],
                    "submitted_by": row.get("submitted_by", ""),
                    "submitted_at": row.get("submitted_at", ""),
                    "user_id": str(row.get("user_id", "")),
                    "chat_id": str(row.get("chat_id", "")),
                    "total": row["total"],
                    "review": row["review"],
                    "status": "committed",
                }
                if current != migrated:
                    metadata[key] = migrated
                    changed = True
        if changed:
            write_json(self.metadata_file, metadata)

    @staticmethod
    def _validate_destination_entries(source, entries):
        allowed_rates = dict(SOURCE_RATES[source])
        for rate, rows in entries.items():
            if rate not in allowed_rates:
                raise ValueError(
                    f"This {source} month contains the {rate} Ks rate, which belongs on the other sheet. "
                    "Move those rows before recording more files in this month."
                )
            for row in rows:
                if source == "LK":
                    if row["total"] or row["review"]:
                        raise ValueError("LK entries must not contain fees.")
                    continue
                tag = detect_rate_tag(row["file_name"])
                if tag and (tag["source"] != source or tag["rate"] != rate):
                    raise ValueError(
                        f"{row['file_name']} has a {tag['tag']} rate tag for {tag['source']}, "
                        f"but is listed in {source}. Move that row to its matching sheet."
                    )

    # ── Rendering ──
    def _build_rows(self, source: str, month: str, entries_by_rate: dict[int, list[dict]]):
        rows: list[list] = []
        bold_rows: list[int] = []

        def add(row, bold=False):
            rows.append(row)
            if bold:
                bold_rows.append(len(rows))

        add([f"{source} SALARY — {month}"], bold=True)
        add([])

        if source == "LK":
            entries = sorted(entries_by_rate.get(0, []),
                             key=lambda entry: (entry["file_name"].casefold(), entry["file_name"]))
            add(SOURCE_COLUMNS[source], bold=True)
            for entry in entries:
                add([entry["file_name"], entry["lines"]])
            add([])
            add(["Total line count", sum(entry["lines"] for entry in entries)], bold=True)
            add(["File count", len(entries)], bold=True)
            return rows, bold_rows, 0

        overall = 0
        for rate, tag in SOURCE_RATES[source]:
            entries = entries_by_rate.get(rate, [])
            # "total" holds the BASE fee (lines × rate); Review is separate, so
            # the columns add up exactly to the grand total.
            base_sum = sum(e["total"] for e in entries)
            review_sum = sum(e["review"] for e in entries)
            table_total = base_sum + review_sum
            overall += table_total

            add([f"Rate: {rate} Ks per line (tag: {tag})"], bold=True)
            add(SOURCE_COLUMNS[source], bold=True)
            for e in entries:
                if source == "NAS":
                    add([e["file_name"], e["lines"], e["total"], e["review"] or ""])
                else:
                    add([e["file_name"], e["lines"], e["total"]])
            add([])
            add(["Total line count", sum(e["lines"] for e in entries)], bold=True)
            if source == "NAS":
                add(["Review count", sum(1 for e in entries if e["review"])], bold=True)
                add(["Total review fee (Ks)", review_sum], bold=True)
                add(["Grand total fee (Ks)", table_total], bold=True)
            else:
                add(["Total fee (Ks)", table_total], bold=True)
            add([])

        add(["OVERALL GRAND TOTAL (all rates) (Ks)", overall], bold=True)
        return rows, bold_rows, overall

    def _read_month(self, source, month, create=True):
        if source not in SOURCE_RATES or normalize_month(month) != month:
            raise ValueError("Invalid destination or month")
        doc = self.get_document(source)
        try:
            ws = doc.worksheet(month)
        except gspread.WorksheetNotFound:
            if not create:
                return doc, None, [], {}
            ws = doc.add_worksheet(month, rows=100, cols=2 if source == "LK" else 10)
        values = ws.get_all_values(value_render_option="FORMULA")
        parsed = self._parse_tab(values)
        for other in SOURCE_RATES:
            if other != source and (parsed[other] or any(
                    row and str(row[0]).startswith(other + " SALARY") for row in values)):
                raise ValueError("This tab belongs to the other salary destination.")
        self._migrate_legacy_metadata(source, month, parsed[source])
        self._validate_destination_entries(source, parsed[source])
        return doc, ws, values, parsed[source]

    def _write_month(self, doc, ws, source, month, previous, entries):
        rows, bold_rows, overall = self._build_rows(source, month, entries)
        columns = len(SOURCE_COLUMNS[source])
        backup = self.backup_dir / f"{source}-{month}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}-{uuid4().hex[:8]}.json"
        write_json(backup, {"spreadsheet_id": doc.id, "worksheet_id": ws.id,
                           "month": month, "source": source, "values": previous})
        row_count = max(ws.row_count, len(rows))
        col_count = max(ws.col_count, columns)
        cell_rows = []
        for number, row in enumerate(rows, 1):
            cells = []
            for column in range(columns):
                value = row[column] if column < len(row) else ""
                cell = {"userEnteredFormat": {"textFormat": {"bold": number in bold_rows}}}
                if source == "LK":
                    marked = (column == 0 and number not in bold_rows and row
                              and LK_HIGHLIGHT_PATTERN.search(str(row[0])))
                    cell["userEnteredFormat"]["backgroundColor"] = (
                        {"red": 1, "green": 0.95, "blue": 0.6} if marked
                        else {"red": 1, "green": 1, "blue": 1})
                if value != "":
                    cell["userEnteredValue"] = ({"numberValue": value} if isinstance(value, int)
                                                else {"stringValue": str(value)})
                cells.append(cell)
            cell_rows.append({"values": cells})
        # updateCells clears uncovered cells inside the range as part of this
        # same atomic request. No separate clear(), resize(), or value write.
        body = {"requests": [
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {
                "rowCount": row_count, "columnCount": col_count}},
                "fields": "gridProperties.rowCount,gridProperties.columnCount"}},
            {"updateCells": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": row_count,
                "startColumnIndex": 0, "endColumnIndex": col_count}, "rows": cell_rows,
                "fields": "userEnteredValue,userEnteredFormat.textFormat.bold"
                          + (",userEnteredFormat.backgroundColor" if source == "LK" else "")}}
        ]}
        if source == "LK":
            body["requests"].extend([
                {"updateSheetProperties": {"properties": {"sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": 3}}, "fields": "gridProperties.frozenRowCount"}},
                {"updateDimensionProperties": {"range": {"sheetId": ws.id,
                    "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                    "properties": {"pixelSize": 620}, "fields": "pixelSize"}},
                {"updateDimensionProperties": {"range": {"sheetId": ws.id,
                    "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                    "properties": {"pixelSize": 140}, "fields": "pixelSize"}},
            ])
        _with_retry(doc.batch_update, body)
        return overall

    def record(self, entry: dict, month: str) -> dict:
        source = entry["source"]
        if source not in SOURCE_RATES:
            raise ValueError("Invalid destination")
        if entry["rate"] not in dict(SOURCE_RATES[source]) or entry["lines"] <= 0:
            raise ValueError("Invalid rate or zero-line salary entry")
        if entry["base_fee"] != entry["rate"] * entry["lines"]:
            raise ValueError("Invalid base fee")
        if source == "LK" and (entry["base_fee"] or entry["review_fee"] or entry["total_fee"]):
            raise ValueError("LK entries must not contain fees.")
        doc, ws, values, entries = self._read_month(source, month)
        existing = [row for table in entries.values() for row in table]
        if entry.get("record_id") and any(row.get("record_id") == entry["record_id"] for row in existing):
            return {"recovered": True, "spreadsheet_id": doc.id}
        if find_duplicate(entries, entry["file_name"]):
            record_id = str(entry.get("record_id") or "")
            metadata = self._load_metadata()
            key = self._metadata_key(source, month, record_id) if record_id else ""
            pending = metadata.get(key)
            if (pending and pending.get("status") in {"pending", "committed"}
                    and pending.get("file_name") == entry["file_name"]):
                pending["status"] = "committed"
                write_json(self.metadata_file, metadata)
                return {"recovered": True, "spreadsheet_id": doc.id}
            return {"duplicate": True, "spreadsheet_id": doc.id}
        row = {"file_name": entry["file_name"], "lines": entry["lines"],
               "total": entry["base_fee"], "review": entry["review_fee"]}
        entries.setdefault(entry["rate"], []).append(row)
        self._store_entry_metadata(source, month, entry, "pending")
        overall = self._write_month(doc, ws, source, month, values, entries)
        self._store_entry_metadata(source, month, entry, "committed")
        return {"overall_total": overall, "spreadsheet_id": doc.id}

    def month_summary(self, source: str, month: str) -> dict:
        """Summarize recorded files for one destination and month."""
        _, _, _, entries = self._read_month(source, month, create=False)
        rows = [row for table in entries.values() for row in table]
        return {
            "file_count": len(rows),
            "line_count": sum(row["lines"] for row in rows),
            "review_count": sum(1 for row in rows if row["review"]),
            "review_total": sum(row["review"] for row in rows),
            "overall_total": sum(row["total"] + row["review"] for row in rows),
        }

    def latest_record(self, source, month, user_id, chat_id):
        _, _, _, entries = self._read_month(source, month, create=False)
        current_by_name = {row["file_name"]: row for table in entries.values() for row in table}
        metadata = self._load_metadata()
        owned = []
        for row in metadata.values():
            if (row.get("source") != source or row.get("month") != month
                    or row.get("status") != "committed"
                    or row.get("user_id") != str(user_id)
                    or row.get("chat_id") != str(chat_id)):
                continue
            current = current_by_name.get(row.get("file_name"))
            if current:
                owned.append({**row, "total": current["total"], "review": current["review"]})
        return max(owned, key=lambda row: row.get("submitted_at", ""), default=None)

    def undo_record(self, source, month, record_id, user_id, chat_id):
        doc, ws, values, entries = self._read_month(source, month, create=False)
        metadata = self._load_metadata()
        key = self._metadata_key(source, month, str(record_id))
        record = metadata.get(key)
        if not record or record.get("status") != "committed":
            return False
        if record.get("user_id") != str(user_id) or record.get("chat_id") != str(chat_id):
            raise ValueError("You can only undo your own submissions in this chat.")
        for table in entries.values():
            for row in table:
                if row["file_name"] != record.get("file_name"):
                    continue
                table.remove(row)
                self._write_month(doc, ws, source, month, values, entries)
                metadata.pop(key, None)
                write_json(self.metadata_file, metadata)
                return True
        return False

    def sheet_url(self, source: str) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.get_document(source).id}"
