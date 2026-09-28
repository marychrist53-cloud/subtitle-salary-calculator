from copy import deepcopy
from types import SimpleNamespace

import pytest

from sheets import SalarySheets


class Document:
    id = "test-spreadsheet"

    def __init__(self, values):
        self.values = deepcopy(values)
        self.fail = False
        self.calls = []
        self.ws = SimpleNamespace(id=1, row_count=100, col_count=10,
                                  get_all_values=lambda **kwargs: deepcopy(self.values))

    def worksheet(self, month):
        return self.ws

    def batch_update(self, body):
        self.calls.append(body)
        if self.fail:
            raise RuntimeError("Simulated service failure")
        update = body["requests"][1]["updateCells"]
        self.values = [[str(next(iter(cell.get("userEnteredValue", {"stringValue": ""}).values())))
                        for cell in row["values"]] for row in update["rows"]]


@pytest.fixture
def ledger(tmp_path):
    sheets = SalarySheets("dummy.json", str(tmp_path / "ids.json"))
    rows, _, _ = sheets._build_rows("NAS", "9.2026", {20: [{"file_name": "Old 20ks.srt", "lines": 2, "total": 40, "review": 0}]})
    doc = Document(rows)
    sheets._docs["NAS"] = doc
    return sheets, doc


def entry(record_id="new"):
    return {"status": "ok", "source": "NAS", "file_name": "=literal name 20ks.srt",
            "rate": 20, "lines": 3, "base_fee": 60, "review_fee": 0,
            "record_id": record_id, "user_id": "7", "chat_id": "8", "submitted_by": "Test User",
            "submitted_at": "2026-09-25T00:00:00+00:00"}


def test_failed_write_preserves_old_values_and_creates_backup(ledger):
    sheets, doc = ledger
    before = deepcopy(doc.values)
    doc.fail = True
    with pytest.raises(RuntimeError):
        sheets.record(entry(), "9.2026")
    assert doc.values == before
    assert len(list(sheets.backup_dir.glob("*.json"))) == 1
    assert len(doc.calls[0]["requests"]) == 2
    assert "updateSheetProperties" in doc.calls[0]["requests"][0]
    assert "updateCells" in doc.calls[0]["requests"][1]


def test_metadata_totals_duplicate_recovery_and_literal_strings(ledger):
    sheets, doc = ledger
    assert sheets.record(entry(), "9.2026")["overall_total"] == 100
    found = sheets.latest_record("NAS", "9.2026", 7, 8)
    assert found["submitted_by"] == "Test User"
    assert found["submitted_at"] == entry()["submitted_at"]
    assert sheets.record(entry(), "9.2026")["recovered"]
    assert sheets.record(entry("different"), "9.2026")["duplicate"]
    assert len(doc.calls) == 1
    serialized_cells = doc.calls[0]["requests"][1]["updateCells"]["rows"]
    assert any(cell.get("userEnteredValue", {}).get("stringValue") == entry()["file_name"]
               for row in serialized_cells for cell in row["values"])


def test_month_summary_counts_lines_files_review_and_overall_total(ledger):
    sheets, _ = ledger
    review_entry = entry()
    review_entry["review_fee"] = 1500
    sheets.record(review_entry, "9.2026")

    assert sheets.month_summary("NAS", "9.2026") == {
        "file_count": 2,
        "line_count": 5,
        "review_count": 1,
        "review_total": 1500,
        "overall_total": 1600,
    }


def test_undo_enforces_owner_and_is_idempotent(ledger):
    sheets, doc = ledger
    sheets.record(entry(), "9.2026")
    assert sheets.latest_record("NAS", "9.2026", 9, 8) is None
    with pytest.raises(ValueError):
        sheets.undo_record("NAS", "9.2026", "new", 9, 8)
    assert sheets.undo_record("NAS", "9.2026", "new", 7, 8)
    assert not sheets.undo_record("NAS", "9.2026", "new", 7, 8)
    assert sheets.latest_record("NAS", "9.2026", 7, 8) is None
    assert sheets._parse_tab(doc.values)["NAS"][20][0]["file_name"] == "Old 20ks.srt"


@pytest.mark.parametrize("damage", ["unknown row", "bad amount", "wrong source"])
def test_unrecognized_existing_data_is_never_overwritten(ledger, damage):
    sheets, doc = ledger
    if damage == "unknown row":
        doc.values.append(["My personal note"])
    elif damage == "bad amount":
        doc.values[4][2] = "=SUM(1,2)"
    else:
        doc.values[0][0] = "PP SALARY — 9.2026"
    with pytest.raises(ValueError):
        sheets.record(entry(), "9.2026")
    assert doc.calls == []


def test_legacy_columns_are_read_without_inventing_submitters(ledger):
    sheets, doc = ledger
    legacy = [row[:4] for row in doc.values]
    parsed = sheets._parse_tab(legacy)["NAS"][20][0]
    assert parsed == {"file_name": "Old 20ks.srt", "lines": 2, "total": 40, "review": 0}


def test_lk_sort_highlight_duplicate_summary_and_undo(tmp_path):
    from logic import analyze_file

    sheets = SalarySheets("dummy.json", str(tmp_path / "ids.json"))
    doc = Document([])
    sheets._docs["LK"] = doc
    names = ["zulu.srt", "Beta CHECKED.srt", "alpha FIX.srt", "beta.srt"]
    for i, name in enumerate(names):
        item = analyze_file("1\n00:00:01,000 --> 00:00:02,000\nမြန်မာ", name, destination="LK")
        item.update(record_id=str(i), user_id="7", chat_id="8", submitted_at=f"2026-09-26T00:00:0{i}")
        sheets.record(item, "9.2026")
    parsed = sheets._parse_tab(doc.values)["LK"][0]
    assert [row["file_name"] for row in parsed] == ["alpha FIX.srt", "Beta CHECKED.srt", "beta.srt", "zulu.srt"]
    assert all(len(row) == 2 for row in doc.values if row)
    assert not any("Ks" in cell or "Rate:" in cell for row in doc.values for cell in row)
    cells = doc.calls[-1]["requests"][1]["updateCells"]["rows"]
    marked = [row["values"][0]["userEnteredValue"]["stringValue"] for row in cells
              if row["values"][0]["userEnteredFormat"]["backgroundColor"]["blue"] < 1]
    assert marked == ["alpha FIX.srt", "Beta CHECKED.srt"]
    item["record_id"] = "another-upload"
    assert sheets.record(item, "9.2026")["duplicate"]
    assert len(doc.calls) == 4
    assert sheets.month_summary("LK", "9.2026") == {
        "file_count": 4, "line_count": 4, "review_count": 0, "review_total": 0, "overall_total": 0}
    with pytest.raises(ValueError, match="own submissions"):
        sheets.undo_record("LK", "9.2026", "2", 9, 8)
    assert sheets.undo_record("LK", "9.2026", "2", 7, 8)
    assert sheets.month_summary("LK", "9.2026")["line_count"] == 3
    assert sheets.latest_record("LK", "9.2026", 7, 8)["file_name"] == "beta.srt"
    cells = doc.calls[-1]["requests"][1]["updateCells"]["rows"]
    assert cells[3]["values"][0]["userEnteredValue"]["stringValue"] == "Beta CHECKED.srt"
    assert cells[4]["values"][0]["userEnteredFormat"]["backgroundColor"]["blue"] == 1


@pytest.mark.parametrize("name", ["FIX.srt", "FiXeD.srt", "check.srt", "CHECKED.srt"])
def test_lk_revision_names_highlight_case_insensitively(name):
    from sheets import LK_HIGHLIGHT_PATTERN
    assert LK_HIGHLIGHT_PATTERN.search(name)
