"""C1/I1: nothing that gets published may carry personal data or infrastructure ids.

Scope: every git-tracked file except docs/superpowers/** and CONTEST_DECLARATION.md.
Generic patterns always run; the local denylist (tests/.pii-denylist.txt, gitignored) runs
only when the file exists. Sample strings below are assembled at runtime so this file does
not match its own patterns.
"""
import subprocess

import pytest

import leakscan
from leakscan import ROOT

pytestmark = pytest.mark.skipif(not leakscan.is_git_checkout(), reason="not a git checkout")


def test_scan_covers_every_published_file():
    files = leakscan.published_files()
    for must in ("README.md", "MCP_INTEGRATION.md", "deploy/deploy.sh", "deploy/Caddyfile",
                 "app/mock_data/query-meals.json", "tests/test_no_leaks.py", "tests/leakscan.py",
                 "web/host.js", "scripts/make_mock_data.py", ".gitignore"):
        assert must in files, must
    assert not [f for f in files if f.startswith("docs/superpowers/")]
    assert "CONTEST_DECLARATION.md" not in files


def test_patterns_catch_known_shapes():
    phone = "1" + "38" + "0013" + "8000"
    acct = "1234" * 3
    inst = "i-0" + "abcdef12345"
    samples = {
        "cn_mobile": f"tel {phone}.",
        "mcd_pay_page": "https://" + "m.mcd" + ".cn/pay?x=1",
        "aws_account_id": f"arn:aws:iam::{acct}:role/x",
        "ec2_instance_id": f"instance {inst}",
        "amazon_com": "someone@" + "amazon" + ".com",
        "aws_dev": "gitlab." + "aws" + ".dev/x",
    }
    for name, text in samples.items():
        assert (name, 1) in leakscan.generic_hits(text), name
    # digit boundaries: longer runs (ms timestamps, 13-digit ids) are not phones / accounts
    assert leakscan.generic_hits(phone + "00") == []
    assert leakscan.generic_hits("x" + acct + "9") == []
    # a public menu photo URL with a timestamp is ignored; a phone right after it is not
    img = "https://menu-img.mcd.cn/pcm/prod/menu/MS_99_320_" + phone + ".png"
    assert ("cn_mobile", 1) in leakscan.generic_hits(img.replace("https://menu-img.", "https://x."))
    assert leakscan.generic_hits(f'"{img}"') == []
    assert ("cn_mobile", 1) in leakscan.generic_hits(f'"{img}" {phone}')


def test_denylist_matching_is_substring_and_case_insensitive():
    assert leakscan.denylist_hits("a\nXyZ road\n", ["xyz"]) == [(0, 2)]
    assert leakscan.denylist_hits("nothing here", ["xyz"]) == []


def test_published_files_have_no_generic_leaks():
    hits = []
    for rel in leakscan.published_files():
        text = leakscan.read_text(rel)
        if text is None:
            continue
        hits += [f"{rel}:{line} {name}" for name, line in leakscan.generic_hits(text)]
    assert not hits, "personal data / infrastructure id in published files:\n" + "\n".join(hits)


def test_published_files_have_no_denylisted_strings():
    entries = leakscan.load_denylist()
    if entries is None:
        pytest.skip("no tests/.pii-denylist.txt on this machine")
    hits = []
    for rel in leakscan.published_files():
        text = leakscan.read_text(rel)
        if text is None:
            continue
        # report the entry index, never the denylisted string itself
        hits += [f"{rel}:{line} denylist entry #{i + 1}" for i, line in leakscan.denylist_hits(text, entries)]
    assert not hits, "denylisted strings in published files:\n" + "\n".join(hits)


@pytest.mark.parametrize("path", ["tests/.pii-denylist.txt", "deploy/deploy.env"])
def test_local_only_files_are_gitignored_and_untracked(path):
    r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path])
    assert r.returncode == 0, f"{path} is not gitignored"
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", path],
                             capture_output=True, text=True, check=True).stdout
    assert tracked == "", f"{path} is tracked"
