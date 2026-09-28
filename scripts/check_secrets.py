"""Check Git's prospective files and staged snapshots without printing secret values."""

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb'"type"\s*:\s*"service_account"'),
]
PRIVATE_NAMES = {".env", "bot_settings.json", "sheet_ids.json", "pending_files.json", "months.json"}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    names = set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
    indexed = set(git("ls-files", "--cached", "-z").decode().split("\0"))
    problems = []
    for name in sorted(names):
        path = Path(name)
        if (path.name in PRIVATE_NAMES or "backups" in path.parts or
                "service-account" in path.name or path.suffix in {".pem", ".key"}):
            problems.append(f"Private runtime file included: {name}")
            continue
        snapshots = []
        if (ROOT / path).is_file():
            snapshots.append((ROOT / path).read_bytes())
        if name in indexed:
            snapshots.append(git("show", ":" + name))
        if any(pattern.search(data) for data in snapshots for pattern in PATTERNS):
            problems.append(f"Possible credential found: {name}")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"Checked {len(names)} Git candidate files: no recognized credentials or private runtime files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
