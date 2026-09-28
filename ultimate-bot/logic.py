"""Core salary-calculator logic: language detection, line counting,
rate tags, source (NAS/PP) detection and review fees.

Shared by the Telegram bot and tests. No Telegram / Google imports here.
"""

import re
from pathlib import Path

# ─── Language detection (Unicode ranges) ───
MYANMAR_PATTERN = re.compile(r"[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]")
LATIN_PATTERN = re.compile(r"[a-zA-Z]")
CHINESE_PATTERN = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")

# ─── Rate tags: tag -> (rate in Ks, source, display label) ───
# Checked in order; the first match wins. "ks" tags are checked before the
# parenthesised PP tags so e.g. "movie(v) 20ks" is treated as NAS.
RATE_TAGS = [
    (r"(?<!\d)25ks(?![a-z0-9])", 25, "NAS", "25ks"),
    (r"(?<!\d)20ks(?![a-z0-9])", 20, "NAS", "20ks"),
    (r"\(\s*v\s*\)", 20, "PP", "(v)"),
    (r"\(\s*p\s*\)", 18, "PP", "(p)"),
    (r"\(\s*mm\s*\)", 22, "PP", "(mm)"),
    (r"\(\s*old\s*\)", 15, "PP", "(old)"),
]

REVIEW_FEE = 1500
SUPPORTED_EXTENSIONS = {".srt", ".ass", ".txt"}

# Explicit p1 / part 1 marker. Boundaries prevent EP1 from being mistaken for p1.
P1_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:p\s*1|part\s*1)(?![a-z0-9])", re.IGNORECASE
)
# Series-shaped names: S01E02, ep4, e3, 2-1
SERIES_PATTERN = re.compile(
    r"s\d+[\s_.-]*e\s*\d+"                   # S01E02 / S01E 02
    r"|(?:^|[\s_.\-])(?:ep|e)\s*\d+"        # ep4 / ep 4 / e3
    r"|(?:^|[\s_.\-])(?:episode)\s*\d+"       # episode 4
    r"|(?:^|[\s_.-])\d+-\d+(?:[\s_.-]|$)",    # 2-1
    re.IGNORECASE,
)
# First episode: E1, E01, EP1, EP01, ep 1, S02E01, 1-1 ... (any season)
EP1_PATTERN = re.compile(
    r"s\d+[\s_.-]*e\s*0?1(?!\d)"             # S01E01 / S02E 1
    r"|(?:^|[\s_.\-])(?:ep|e)\s*0?1(?!\d)"  # ep1 / e01 / ep 1
    r"|(?:^|[\s_.\-])(?:episode)\s*0?1(?!\d)" # episode 1
    r"|(?:^|[\s_.-])\d+-0?1(?!\d)",           # 2-1
    re.IGNORECASE,
)

SRT_TIMECODE_PATTERN = re.compile(
    r"^(\d{1,}:[0-5]\d:[0-5]\d[,.]\d{1,3})\s*-->\s*"
    r"(\d{1,}:[0-5]\d:[0-5]\d[,.]\d{1,3})(?:\s+.*)?$"
)
SRT_CUE_ID_PATTERN = re.compile(r"^\d+(?:[-.]\d+)*$")


def decode_bytes(data: bytes | bytearray) -> str:
    raw = bytes(data)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if b"\x00" in raw:
        # BOM-less UTF-16 subtitle files generally have ASCII cue/timecode bytes.
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            if MYANMAR_PATTERN.search(text) and not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
                return text
        raise ValueError("Invalid text encoding. Save the subtitles as UTF-8 or UTF-16.")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Unsupported text encoding")


def _visible(text: str) -> str:
    text = re.sub(r"\{[^}]*\}|<[^>]*>", "", text)
    return text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ").strip()


def _milliseconds(timestamp: str) -> int:
    h, m, s, ms = re.split(r"[:,.]", timestamp)
    h, m, s = int(h), int(m), int(s)
    ms = int(ms.ljust(3, "0"))
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def subtitle_cues(text: str, file_name: str) -> list[str]:
    """Validate every cue and return visible dialogue, retaining empty SRT cues."""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", normalized):
        raise ValueError("The file contains binary/control characters.")
    suffix = Path(file_name).suffix.lower()
    cues = []
    if suffix == ".srt":
        lines = normalized.splitlines()
        timecodes = []
        for index, line in enumerate(lines):
            timing = SRT_TIMECODE_PATTERN.fullmatch(line.strip())
            if timing:
                timecodes.append((index, timing))
            elif "-->" in line:
                raise ValueError("Malformed SRT cue: expected a number and valid timestamps.")
        if not timecodes:
            raise ValueError("Malformed SRT cue: expected a number and valid timestamps.")

        # SRT identifiers are optional, and real-world files often use IDs such
        # as "34-1". Parse timing lines as cue boundaries so missing separator
        # blank lines don't merge otherwise valid cues into one malformed block.
        prefix = [line.strip() for line in lines[:timecodes[0][0]] if line.strip()]
        if len(prefix) > 1:
            raise ValueError("Malformed SRT cue: expected a number and valid timestamps.")

        for cue_index, (line_index, timing) in enumerate(timecodes):
            if _milliseconds(timing[1]) >= _milliseconds(timing[2]):
                raise ValueError("SRT cue end time must be after its start time.")

            next_index = timecodes[cue_index + 1][0] if cue_index + 1 < len(timecodes) else len(lines)
            payload_lines = lines[line_index + 1:next_index]

            # A blank line ends an SRT cue. If the next cue follows without a
            # blank separator, its numeric ID may be the last line before the
            # next timestamp instead.
            trimmed_end = len(payload_lines)
            while trimmed_end and not payload_lines[trimmed_end - 1].strip():
                trimmed_end -= 1
            blank_positions = [i for i, line in enumerate(payload_lines[:trimmed_end]) if not line.strip()]
            if blank_positions:
                first_blank = blank_positions[0]
                after_separator = [line.strip() for line in payload_lines[blank_positions[-1] + 1:trimmed_end]
                                   if line.strip()]
                if after_separator:
                    is_next_id = (cue_index + 1 < len(timecodes) and len(after_separator) == 1
                                  and SRT_CUE_ID_PATTERN.fullmatch(after_separator[0]))
                    if not is_next_id:
                        raise ValueError("Malformed SRT cue: expected a number and valid timestamps.")
                payload_lines = payload_lines[:first_blank]
            elif (cue_index + 1 < len(timecodes) and trimmed_end
                  and SRT_CUE_ID_PATTERN.fullmatch(payload_lines[trimmed_end - 1].strip())):
                payload_lines = payload_lines[:trimmed_end - 1]

            payload = _visible("\n".join(payload_lines))
            # A numbered/timed SRT cue may intentionally have blank dialogue.
            # Retain it so it counts as one subtitle line, while language
            # detection still uses only visible non-empty dialogue.
            cues.append(payload)
    elif suffix == ".ass":
        fields = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
        in_events = False
        for line in normalized.splitlines():
            line = line.strip()
            if line.startswith("["):
                in_events = line.lower() == "[events]"
            if in_events and line.lower().startswith("format:"):
                fields = [part.strip().lower() for part in line.split(":", 1)[1].split(",")]
            if line.lower().startswith("dialogue:"):
                if not fields or fields[-1] != "text":
                    raise ValueError("ASS events must have a Text field as the last column.")
                values = line.split(":", 1)[1].split(",", len(fields) - 1)
                if len(values) != len(fields) or not _visible(values[-1]):
                    raise ValueError("Malformed or empty ASS dialogue event.")
                cues.append(_visible(values[-1]))
    elif suffix == ".txt":
        cues = [value for line in normalized.splitlines() if (value := _visible(line))]
    else:
        raise ValueError("Unsupported file type. Send an .srt, .ass, or .txt subtitle file.")
    if not cues:
        raise ValueError("The file has no valid subtitle lines. Nothing was logged.")
    return cues


# ─── Language ───
def analyze_language(text: str) -> dict:
    has_myanmar = bool(MYANMAR_PATTERN.search(text))
    has_latin = bool(LATIN_PATTERN.search(text))
    has_chinese = bool(CHINESE_PATTERN.search(text))
    return {
        "has_myanmar": has_myanmar,
        "has_latin": has_latin,
        "has_chinese": has_chinese,
        "english_only": has_latin and not has_myanmar,
        "chinese_only": has_chinese and not has_myanmar,
    }


def untranslated_reason(text: str) -> str | None:
    """Return a human-readable reason if the file is untranslated (no Burmese)."""
    a = analyze_language(text)
    if a["english_only"]:
        return "English only — no Myanmar (Burmese) text found, file is untranslated."
    if a["chinese_only"]:
        return "Chinese only — no Myanmar (Burmese) text found, file is untranslated."
    return None


# ─── Line counting ───
def count_lines_srt(text: str) -> int:
    try:
        return len(subtitle_cues(text, "subtitle.srt"))
    except ValueError:
        return 0


def count_lines_ass(text: str) -> int:
    try:
        return len(subtitle_cues(text, "subtitle.ass"))
    except ValueError:
        return 0


def count_lines_txt(text: str) -> int:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return 0
    return sum(1 for line in normalized.split("\n") if line.strip())


def count_lines(text: str, file_name: str) -> int:
    lower = file_name.lower()
    if lower.endswith(".srt"):
        return count_lines_srt(text)
    if lower.endswith(".ass"):
        return count_lines_ass(text)
    if lower.endswith(".txt"):
        return count_lines_txt(text)
    return 0


# ─── Rate tag / source ───
def detect_rate_tag(file_name: str) -> dict | None:
    """Find the rate tag in the filename.

    Returns {"rate": int, "source": "NAS"|"PP", "tag": "20ks"|"(v)"|...} or None.
    """
    lower = file_name.lower()
    for pattern, rate, source, label in RATE_TAGS:
        if re.search(pattern, lower):
            return {"rate": rate, "source": source, "tag": label}
    return None


# ─── Review fee ───
def review_fee_for(file_name: str) -> int:
    """+1500 when the name contains p1/part1 of a movie, or of the first
    episode of a series (any season — seasons are not checked)."""
    if not P1_PATTERN.search(file_name):
        return 0
    if SERIES_PATTERN.search(file_name):
        return REVIEW_FEE if EP1_PATTERN.search(file_name) else 0
    return REVIEW_FEE


# ─── One-stop analysis ───
def analyze_file(text: str, file_name: str, *, destination: str | None = None,
                 require_rate: bool = True) -> dict:
    """Full analysis of one subtitle file.

    Returns a dict with either:
      status="rejected" + reason          (untranslated)
      status="error"    + reason          (no rate tag)
      status="ok"       + full breakdown
    """
    extension = Path(file_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        return {
            "status": "error",
            "reason": "Unsupported file type. Send an .srt, .ass, or .txt subtitle file.",
        }

    try:
        cues = subtitle_cues(text, file_name)
    except ValueError as exc:
        return {"status": "error", "reason": str(exc)}
    dialogue = "\n".join(cues)
    reason = untranslated_reason(dialogue)
    if reason:
        return {"status": "rejected", "reason": reason}
    if not MYANMAR_PATTERN.search(dialogue):
        return {"status": "rejected", "reason": "No Myanmar (Burmese) subtitle text found."}

    # LK records counts only, even when a filename happens to include a rate tag.
    tag = None if destination == "LK" else detect_rate_tag(file_name)
    if tag is None and destination != "LK" and require_rate:
        return {
            "status": "error",
            "reason": (
                "No rate tag in filename. NAS: 20ks or 25ks; PP: (v), (p), (mm), or (old)."
            ),
        }

    lines = len(cues)
    rate = tag["rate"] if tag else 0
    source = "LK" if destination == "LK" else tag["source"] if tag else None
    base_fee = lines * rate
    # PP has no review fee; review pay is tracked on NAS only.
    review = review_fee_for(file_name) if source == "NAS" else 0

    return {
        "status": "ok",
        "file_name": file_name,
        "source": source,
        "tag": tag["tag"] if tag else "",
        "rate": rate,
        "lines": lines,
        "base_fee": base_fee,
        "review_fee": review,
        "total_fee": base_fee + review,
    }
