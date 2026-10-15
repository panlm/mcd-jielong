"""Helpers for the leak tests (C1/I1): generic personal-data / infrastructure-id patterns,
plus an optional local denylist that is never committed.

Nothing here contains real personal data: the patterns are generic shapes, and the real
strings (if any) live only in the untracked, gitignored tests/.pii-denylist.txt.
Failure messages report file:line and the pattern name, never the matched text.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENYLIST = ROOT / "tests" / ".pii-denylist.txt"
# docs/superpowers/** is internal working material that is not published;
# CONTEST_DECLARATION.md is the official text, pinned byte-for-byte by sha256.
EXCLUDED_PREFIXES = ("docs/superpowers/",)
EXCLUDED_FILES = frozenset({"CONTEST_DECLARATION.md"})

# Public product photos carry 13-digit millisecond timestamps: drop those URLs before the
# digit patterns run, so the patterns can stay strict.
_MENU_IMG = re.compile(r"https://menu-img\.mcd\.cn/[^\s\"'<>)\]]*")

PATTERNS = {
    "cn_mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "mcd_pay_page": re.compile(r"m\.mcd\.cn/"),
    "aws_account_id": re.compile(r"(?<!\d)\d{12}(?!\d)"),
    "ec2_instance_id": re.compile(r"i-0[0-9a-f]{8,}"),
    "amazon_com": re.compile(r"amazon\.com", re.I),
    "aws_dev": re.compile(r"aws\.dev", re.I),
}


def strip_public_images(text: str) -> str:
    return _MENU_IMG.sub("", text)


def generic_hits(text: str) -> list[tuple[str, int]]:
    """[(pattern_name, line_no)] for every generic pattern found in text."""
    out = []
    for no, line in enumerate(strip_public_images(text).splitlines(), 1):
        for name, pat in PATTERNS.items():
            if pat.search(line):
                out.append((name, no))
    return out


def load_denylist() -> list[str] | None:
    """Entries of tests/.pii-denylist.txt (one string per line), or None if the file is absent."""
    if not DENYLIST.is_file():
        return None
    entries = [ln.strip() for ln in DENYLIST.read_text(encoding="utf-8").splitlines()]
    return [e for e in entries if e]


def denylist_hits(text: str, entries: list[str]) -> list[tuple[int, int]]:
    """[(entry_index, line_no)] for every denylisted string found in text (case-insensitive)."""
    out = []
    folded = [e.casefold() for e in entries]
    for no, line in enumerate(text.casefold().splitlines(), 1):
        for i, e in enumerate(folded):
            if e in line:
                out.append((i, no))
    return out


def is_git_checkout() -> bool:
    return (ROOT / ".git").exists()


def published_files() -> list[str]:
    """Every git-tracked file that ends up in a public repo (relative paths)."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    names = [n for n in out.split("\0") if n]
    return [n for n in names if not n.startswith(EXCLUDED_PREFIXES) and n not in EXCLUDED_FILES]


def read_text(rel: str) -> str | None:
    """File content as text, or None for a binary (non-UTF-8) file."""
    try:
        return (ROOT / rel).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
