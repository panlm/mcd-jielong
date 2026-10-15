import hashlib
import json
import re
from pathlib import Path

import leakscan
from app.mcp_client import ALLOWED_TOOLS

ROOT = Path(__file__).resolve().parent.parent
DECLARATION_SHA256 = "cd28101bcb29cc8fb24b372fe91ce6b42c4a1487a54e01f7ade0c8c6a84241b7"
REQUIRED = ["README.md", "CONTEST_DECLARATION.md", "MCP_INTEGRATION.md", "mcp-config.example.json"]


def test_contest_declaration_verbatim():
    assert hashlib.sha256((ROOT / "CONTEST_DECLARATION.md").read_bytes()).hexdigest() == DECLARATION_SHA256


def test_required_files_exist():
    missing = [f for f in REQUIRED if not (ROOT / f).is_file()]
    assert not missing, f"missing contest files: {missing}"


def test_mcp_config_example_uses_env_placeholder_only():
    text = (ROOT / "mcp-config.example.json").read_text(encoding="utf-8")
    cfg = json.loads(text)
    assert cfg["mcpServers"]["mcd-mcp"]["headers"]["Authorization"] == "Bearer ${MCD_MCP_TOKEN}"
    assert not re.search(r"[A-Za-z0-9]{24,}", text), "long token-like string in example config"


def test_integration_doc_lists_every_whitelisted_tool():
    doc = (ROOT / "MCP_INTEGRATION.md").read_text(encoding="utf-8")
    missing = sorted(t for t in ALLOWED_TOOLS if t not in doc)
    assert not missing, f"tools not documented: {missing}"


def test_docs_leak_no_private_strings():
    """The contest docs: generic personal-data / infra-id patterns, the optional local
    denylist (tests/.pii-denylist.txt), and our SSM parameter paths. The repo-wide scan in
    test_no_leaks.py covers every other published file."""
    entries = leakscan.load_denylist() or []
    for name in ("README.md", "MCP_INTEGRATION.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not leakscan.generic_hits(text), (name, leakscan.generic_hits(text))
        assert not leakscan.denylist_hits(text, entries), name
        assert "/mcd-1024/" not in text, name


def test_docs_only_link_to_allowed_hosts():
    allowed = ("https://mcd.aws.panlm.click", "https://open.mcd.cn/",
               "https://github.com/M-China/", "https://mcp.mcd.cn",
               "https://github.com/panlm/mcd-jielong",
               # deployment placeholders, not links
               "http://127.0.0.1", "https://your.domain")
    for name in ("README.md", "MCP_INTEGRATION.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for url in re.findall(r"https?://[^\s<>)\"'`]+", text):
            assert url.startswith(allowed), f"{name}: unexpected URL {url}"


def test_docs_login_link_uses_fragment_not_query():
    for name in ("README.md", "MCP_INTEGRATION.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "host?k=" not in text, name
    assert "/host#k=" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_headline_verbatim_and_savings_explained():
    t = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "**加班点麦当劳，群里发个链接接龙，一个人请客合一单，省下 N 份配送费。**" in t
    body = t.split("## 为什么合成一单", 1)[1].split("\n## ", 1)[0]
    assert "N 份配送费" in body and "N−1" in body and "N-1" not in body


def test_integration_doc_lists_follow_up_query_order_calls():
    doc = (ROOT / "MCP_INTEGRATION.md").read_text(encoding="utf-8")
    row = next(ln for ln in doc.splitlines() if ln.startswith("| `query-order`"))
    assert "刷新状态" in row and "就是这单" in row and "取消订单" in row
    cancel = next(ln for ln in doc.splitlines() if ln.startswith("| `cancel-order`"))
    assert "query-order" in cancel


def test_docs_describe_demo_isolation_accurately():
    for name in ("README.md", "MCP_INTEGRATION.md"):
        t = (ROOT / name).read_text(encoding="utf-8")
        assert "物理上" not in t, name
        assert "单独的系统用户" in t and "实例元数据服务" in t and "/demo/" in t, name
