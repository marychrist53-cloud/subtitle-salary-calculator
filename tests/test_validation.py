import pytest

from logic import analyze_file, decode_bytes, detect_rate_tag, review_fee_for


SRT = "1\n00:00:01,000 --> 00:00:02,000\nမြန်မာ\n"


@pytest.mark.parametrize(
    ("content", "expected_status"),
    [
        ("", "error"),
        ("မြန်မာ", "error"),
        ("1\n00:00:01,000 --> 00:00:02,000", "rejected"),
        (SRT + "\nbroken", "error"),
    ],
)
def test_invalid_or_empty_srt_cannot_earn_review_fee(content, expected_status):
    result = analyze_file(content, "p1 Movie 20ks.srt")
    assert result["status"] == expected_status
    assert "review_fee" not in result


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"])
def test_unicode_encodings_keep_burmese_and_salary(encoding):
    result = analyze_file(decode_bytes(SRT.encode(encoding)), "Movie p1 20ks.srt")
    assert result["total_fee"] == 1520


@pytest.mark.parametrize("timestamps", ["00:61:01,000 --> 00:62:01,000", "00:00:03,000 --> 00:00:02,000"])
def test_invalid_timing_rejected(timestamps):
    assert analyze_file(f"1\n{timestamps}\nမြန်မာ", "p1 Movie 20ks.srt")["status"] == "error"


def test_ass_metadata_does_not_count_as_translation():
    text = "[Script Info]\nTitle: မြန်မာ\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello world"
    assert analyze_file(text, "Movie 20ks.ass")["status"] == "rejected"


def test_real_ass_dialogue_with_comma_counts_once():
    text = "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\b1}မြန်မာ, hello"
    assert analyze_file(text, "Movie 20ks.ass")["lines"] == 1


def test_binary_content_and_invalid_rates_rejected():
    assert analyze_file("မြန်မာ\x00", "Movie 20ks.txt")["status"] == "error"
    assert detect_rate_tag("Movie 120ks.srt") is None


@pytest.mark.parametrize("episode", ["e1", "EP1", "e01", "EP 01", "S09E 01"])
def test_explicit_part_marker_and_any_season(episode):
    assert review_fee_for(f"Show part 1 {episode} 20ks.srt") == 1500
    assert review_fee_for(f"Show {episode} 20ks.srt") == 0


def test_movie_and_later_episode_review_rules():
    assert review_fee_for("p 1 Son of blabla 20ks.srt") == 1500
    assert review_fee_for("Show part 1 S09E 02 20ks.srt") == 0


@pytest.mark.parametrize("name", ["Movie p1.srt", "Movie p1(v).srt", "Movie p1 25ks.srt"])
def test_lk_counts_empty_cues_without_rates_or_review(name):
    content = SRT + "\n2\n00:00:02,000 --> 00:00:03,000\n\n"
    result = analyze_file(content, name, destination="LK")
    assert result["status"] == "ok"
    assert result["source"] == "LK" and result["lines"] == 2
    assert result["rate"] == result["base_fee"] == result["review_fee"] == result["total_fee"] == 0
