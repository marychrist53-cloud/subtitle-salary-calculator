"""Render fictional NAS, PP, and LK examples without Telegram or Google access."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ultimate-bot"))

from logic import analyze_file  # noqa: E402
from sheets import SOURCE_RATES, SalarySheets  # noqa: E402


def srt(*dialogue: str) -> str:
    """Make tiny fictional SRT content that exercises cue counting."""
    blocks = []
    for number, text in enumerate(dialogue, 1):
        blocks.extend((str(number), f"00:00:{number:02d},000 --> 00:00:{number + 1:02d},000", text, ""))
    return "\n".join(blocks)


SAMPLES = [
    ("NAS", "Demo Film p1 (20ks).srt", srt("မြန်မာစာ စမ်းသပ်စာကြောင်း ၁", "မြန်မာစာ စမ်းသပ်စာကြောင်း ၂")),
    ("NAS", "Demo Series S04E01 part 1 (25ks).srt", srt(
        "မြန်မာစာ စမ်းသပ်စာကြောင်း ၁", "မြန်မာစာ စမ်းသပ်စာကြောင်း ၂", "မြန်မာစာ စမ်းသပ်စာကြောင်း ၃")),
    ("PP", "Demo Drama ep1 (v).srt", srt("မြန်မာစာ စမ်းသပ်စာကြောင်း ၁", "မြန်မာစာ စမ်းသပ်စာကြောင်း ၂")),
    ("LK", "Demo Project checked.srt", srt("မြန်မာစာ စမ်းသပ်စာကြောင်း ၁", "မြန်မာစာ စမ်းသပ်စာကြောင်း ၂")),
]


def main() -> None:
    entries = {source: {rate: [] for rate, _ in SOURCE_RATES[source]} for source in SOURCE_RATES}
    for destination, file_name, content in SAMPLES:
        result = analyze_file(content, file_name, destination=destination, require_rate=destination != "LK")
        if result["status"] != "ok":
            raise RuntimeError(f"Demo sample failed analysis: {file_name}: {result.get('reason', result['status'])}")
        rate = result["rate"] if destination != "LK" else 0
        entries[destination][rate].append({
            "file_name": result["file_name"],
            "lines": result["lines"],
            "total": result["base_fee"],
            "review": result["review_fee"],
        })

    builder = SalarySheets.__new__(SalarySheets)
    for source in ("NAS", "PP", "LK"):
        rows, _, _ = builder._build_rows(source, "9.2026", entries[source])
        print("\n".join(" | ".join(map(str, row)) for row in rows))
        print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
