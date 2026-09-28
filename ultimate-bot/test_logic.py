"""Sanity tests for logic.py and sheets.py (no network / no credentials needed)."""

from pathlib import Path
from tempfile import TemporaryDirectory

from sheets import SalarySheets, SOURCE_RATES, find_duplicate, normalize_month
from pending_store import load_pending_files, pending_chat_ids, save_pending_files
from logic import (
    analyze_file,
    analyze_language,
    count_lines,
    detect_rate_tag,
    review_fee_for,
)


def srt(n=3):
    blocks = []
    for i in range(1, n + 1):
        blocks.append(f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\nစမ်းသပ် မြန်မာ {i}")
    return "\n\n".join(blocks) + "\n"


def test_language():
    assert analyze_language("hello world")["english_only"] is True
    assert analyze_language("你好 world")["chinese_only"] is True
    assert analyze_language("မင်္ဂလာပါ hello")["english_only"] is False
    assert analyze_language("မြန်မာ")["has_myanmar"] is True


def test_line_counts():
    assert count_lines(srt(3), "a.srt") == 3
    assert count_lines("Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,one\nDialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,two\nComment: x", "a.ass") == 2
    assert count_lines("a\n\nb\n  \nc", "a.txt") == 3
    assert count_lines("", "a.txt") == 0


def test_rate_tags():
    assert detect_rate_tag("AI Girl 2(20ks).srt") == {"rate": 20, "source": "NAS", "tag": "20ks"}
    assert detect_rate_tag("Movie FINAL_25KS.SRT") == {"rate": 25, "source": "NAS", "tag": "25ks"}
    assert detect_rate_tag("Drama ep1 (v).srt") == {"rate": 20, "source": "PP", "tag": "(v)"}
    assert detect_rate_tag("Show (p).srt") == {"rate": 18, "source": "PP", "tag": "(p)"}
    assert detect_rate_tag("Title (mm).srt") == {"rate": 22, "source": "PP", "tag": "(mm)"}
    assert detect_rate_tag("Classic (OLD).srt") == {"rate": 15, "source": "PP", "tag": "(old)"}
    assert detect_rate_tag("no tag here.srt") is None
    # ks tags win over PP tags when both appear
    assert detect_rate_tag("weird (mm) 20ks.srt")["source"] == "NAS"


def test_review_fees():
    # Movie with p1 → fee
    assert review_fee_for("Movie p1(20ks).srt") == 1500
    assert review_fee_for("p1 Son of blabla(20ks).srt") == 1500
    assert review_fee_for("movie PART 1 (v).srt") == 1500
    # Explicit p1/part 1 marker is required; EP1 alone is not a p1 marker.
    assert review_fee_for("Show EP1 (20ks).srt") == 0
    assert review_fee_for("Show S01E01 (20ks).srt") == 0
    # p10 / part12 are not p1
    assert review_fee_for("Movie p10(20ks).srt") == 0
    assert review_fee_for("Series part12(20ks).srt") == 0
    # Series first episode → fee (any season; seasons are not checked)
    assert review_fee_for("Show S01E01 p1(20ks).srt") == 1500
    assert review_fee_for("Show S02E01 p1(20ks).srt") == 1500
    assert review_fee_for("Drama ep1 p1(20ks).srt") == 1500
    assert review_fee_for("Drama EP 1 p1(20ks).srt") == 1500
    for episode_marker in ("e1", "ep1", "e01", "ep01", "E01", "e 1", "ep 1", "e 01", "ep 01"):
        assert review_fee_for(f"Show part 1 {episode_marker}(20ks).srt") == 1500, episode_marker
    assert review_fee_for("Show S02E 01 part 1(20ks).srt") == 1500
    assert review_fee_for("Drama E01 p1(20ks).srt") == 1500
    assert review_fee_for("Drama EP01 p1(20ks).srt") == 1500
    assert review_fee_for("Drama episode 1 p1(20ks).srt") == 1500
    assert review_fee_for("Show 2-1 p1(20ks).srt") == 1500
    # Series later episode → no fee
    assert review_fee_for("Show S01E03 p1(20ks).srt") == 0
    assert review_fee_for("Drama ep5 p1(20ks).srt") == 0
    assert review_fee_for("Show S01E03 p1_e3(20ks).srt") == 0
    # No p1 at all → no fee, even for ep1
    assert review_fee_for("Show S01E01(20ks).srt") == 0


def test_analyze_full():
    # Rejected: english only
    r = analyze_file("1\n00:00:01,000 --> 00:00:02,000\nhello world", "english(20ks).srt")
    assert r["status"] == "rejected" and "English only" in r["reason"]

    # Error: no tag
    r = analyze_file(srt(), "burmese no tag.srt")
    assert r["status"] == "error"

    # OK: NAS with review
    r = analyze_file(srt(10), "Movie S01E01 p1(20ks).srt")
    assert r["status"] == "ok"
    assert r["lines"] == 10 and r["rate"] == 20 and r["source"] == "NAS"
    assert r["base_fee"] == 200 and r["review_fee"] == 1500 and r["total_fee"] == 1700

    # OK: PP without review bookkeeping
    r = analyze_file(srt(4), "Drama (mm).srt")
    assert r["status"] == "ok" and r["source"] == "PP"
    assert r["base_fee"] == 88 and r["review_fee"] == 0 and r["total_fee"] == 88

    r = analyze_file(srt(5), "Show (p).srt")
    assert r["status"] == "ok" and r["source"] == "PP" and r["tag"] == "(p)"
    assert r["rate"] == 18 and r["base_fee"] == 90 and r["review_fee"] == 0


def test_sheet_roundtrip_totals():
    """Build a tab, 'write' it, parse it back, add files, rebuild — totals must
    add up: Total column = base only, grand total = base + review."""
    s = SalarySheets("dummy.json", ids_file="_tmp_ids.json")

    nas_a = {"file_name": "MovieA p1(20ks).srt", "lines": 10, "total": 200, "review": 1500}
    nas_b = {"file_name": "MovieB(20ks).srt", "lines": 5, "total": 100, "review": 0}

    rows, _, overall = s._build_rows("NAS", "8.2026", {20: [nas_a, nas_b], 25: []})
    assert overall == 200 + 100 + 1500, overall
    # Total column holds base only; review is separate
    data_a = rows[4]
    assert data_a[:4] == ["MovieA p1(20ks).srt", 10, 200, 1500], data_a

    # Round-trip: parse the built rows back (as the sheet would return them)
    values = [[str(c) if c != "" else "" for c in r] for r in rows]
    parsed = s._parse_tab(values)["NAS"]
    assert parsed[20] == [nas_a, nas_b], parsed

    # Add a 25ks file and rebuild — overall = previous + new base
    nas_c = {"file_name": "ShowC(25ks).srt", "lines": 8, "total": 200, "review": 0}
    parsed.setdefault(25, []).append(nas_c)
    rows2, _, overall2 = s._build_rows("NAS", "8.2026", parsed)
    assert overall2 == 300 + 1500 + 200, overall2
    # Second round-trip is stable
    values2 = [[str(c) if c != "" else "" for c in r] for r in rows2]
    parsed2 = s._parse_tab(values2)["NAS"]
    assert len(parsed2[20]) == 2 and len(parsed2[25]) == 1
    assert find_duplicate(parsed2, "ShowC(25ks).srt") is True
    assert find_duplicate(parsed2, "ShowD(25ks).srt") is False

    # PP tables have no review anywhere
    pp = {"file_name": "Drama(v).ass", "lines": 100, "total": 2000, "review": 0}
    rows_pp, _, overall_pp = s._build_rows("PP", "8.2026", {20: [pp]})
    assert overall_pp == 2000, overall_pp
    assert rows_pp[4][:3] == ["Drama(v).ass", 100, 2000], rows_pp[4]


def test_rate_tables_remain_separate_by_destination():
    """NAS tags and PP tags must render only in their designated sheets."""
    s = SalarySheets("dummy.json", ids_file="_tmp_ids.json")

    assert dict(SOURCE_RATES["NAS"]) == {20: "20ks", 25: "25ks"}
    assert dict(SOURCE_RATES["PP"]) == {20: "(v)", 18: "(p)", 22: "(mm)", 15: "(old)"}

    nas_rows, _, nas_total = s._build_rows("NAS", "8.2026", {
        20: [{"file_name": "Movie20ks.srt", "lines": 10, "total": 200, "review": 0}],
        25: [{"file_name": "Movie25ks.srt", "lines": 10, "total": 250, "review": 0}],
    })
    nas_values = [[str(c) if c != "" else "" for c in row] for row in nas_rows]
    parsed_nas = s._parse_tab(nas_values)["NAS"]
    assert set(parsed_nas) == {20, 25}
    assert nas_total == 450

    pp_rows, _, pp_total = s._build_rows("PP", "8.2026", {
        20: [{"file_name": "Movie(v).srt", "lines": 10, "total": 200, "review": 0}],
        18: [{"file_name": "Movie(p).srt", "lines": 10, "total": 180, "review": 0}],
        22: [{"file_name": "Movie(mm).srt", "lines": 10, "total": 220, "review": 0}],
        15: [{"file_name": "Movie(old).srt", "lines": 10, "total": 150, "review": 0}],
    })
    pp_values = [[str(c) if c != "" else "" for c in row] for row in pp_rows]
    parsed_pp = s._parse_tab(pp_values)["PP"]
    assert set(parsed_pp) == {20, 18, 22, 15}
    assert pp_total == 750

    try:
        s._validate_destination_entries("NAS", {22: [{"file_name": "Movie(mm).srt"}]})
    except ValueError as exc:
        assert "belongs on the other sheet" in str(exc)
    else:
        raise AssertionError("PP rate unexpectedly accepted in NAS")


def test_duplicates():
    from sheets import find_duplicate
    entries = {20: [{"file_name": "Movie p1(20ks).srt", "lines": 5, "total": 100, "review": 0}]}
    assert find_duplicate(entries, "Movie p1(20ks).srt") is True
    assert find_duplicate(entries, "  Movie p1(20ks).srt  ") is True   # whitespace ok
    assert find_duplicate(entries, "Movie p2(20ks).srt") is False
    assert find_duplicate({}, "anything.srt") is False


def test_normalize_month():
    assert normalize_month("8.2026") == "8.2026"
    assert normalize_month("08/2026") == "8.2026"
    assert normalize_month("12-2026") == "12.2026"
    assert normalize_month("13.2026") is None
    assert normalize_month("august") is None


def test_unsupported_extension():
    entry = analyze_file("မြန်မာ text", "subtitle.pdf")
    assert entry["status"] == "error"
    assert "Unsupported file type" in entry["reason"]


def test_pending_files_persist_per_chat():
    with TemporaryDirectory() as folder:
        store = Path(folder) / "pending_files.json"
        files = [{"file_name": "test.srt", "text": "မြန်မာ", "deleted_note": ""}]
        save_pending_files(store, 123, files)
        save_pending_files(store, 456, [{"file_name": "other.txt", "text": "abc"}])

        assert load_pending_files(store, 123) == files
        assert load_pending_files(store, 456)[0]["file_name"] == "other.txt"
        assert set(pending_chat_ids(store)) == {123, 456}

        save_pending_files(store, 123, [])
        assert load_pending_files(store, 123) == []
        assert pending_chat_ids(store) == [456]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
