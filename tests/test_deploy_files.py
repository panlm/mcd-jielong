"""Static checks for deploy/ files (no network, no AWS)."""
import re
import subprocess
from pathlib import Path

import pytest

D = Path(__file__).resolve().parent.parent / "deploy"


def _unit(name):
    return (D / name).read_text()


def _exec(text):
    return next(l for l in text.splitlines() if l.startswith("ExecStart="))


def test_units_single_process_and_flags():
    for name, port in (("mcd-jielong.service", "8000"), ("mcd-jielong-demo.service", "8001")):
        t = _unit(name)
        ex = _exec(t)
        assert "--workers" not in ex
        assert "--factory" in ex and "app.main:app_from_env" in ex
        assert f"--host 127.0.0.1 --port {port}" in ex
        assert "--proxy-headers --forwarded-allow-ips 127.0.0.1" in ex
        assert "single" in t.lower() and "process" in t.lower()  # R16 comment
        assert "Restart=on-failure" in t
        for k in ("WorkingDirectory=/opt/mcd-jielong/current",
                  "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes"):
            assert k in t
        assert "MCD_TOKEN_FILE" not in t and "MCD_HOST_KEY" not in t


def _directives(text, key):
    return [ln.split("=", 1)[1].strip() for ln in text.splitlines() if ln.startswith(key + "=")]


def test_demo_runs_isolated_from_real_instance():
    """I3: separate system user, no IMDS (so no instance-role credentials, no SSM), and the
    real data directory is not even visible to it. The real unit keeps mcdjl and its IMDS."""
    real, demo = _unit("mcd-jielong.service"), _unit("mcd-jielong-demo.service")
    assert _directives(real, "User") == ["mcdjl"] and _directives(real, "Group") == ["mcdjl"]
    assert _directives(demo, "User") == ["mcdjl-demo"] and _directives(demo, "Group") == ["mcdjl-demo"]
    deny = " ".join(_directives(demo, "IPAddressDeny")).split()
    assert "169.254.169.254" in deny
    assert _directives(demo, "InaccessiblePaths") == ["/var/lib/mcd-jielong"]
    assert _directives(demo, "ReadWritePaths") == ["/var/lib/mcd-jielong-demo"]
    assert not _directives(real, "IPAddressDeny")  # real reads its token from SSM via IMDS
    env = " ".join(_directives(demo, "Environment") + _directives(demo, "EnvironmentFile"))
    assert "MCD_MODE=demo" in env and "AWS_" not in env and "TOKEN" not in env and "KEY" not in env
    assert not _directives(demo, "EnvironmentFile") and not _directives(demo, "LoadCredential")


def test_real_vs_demo_specifics():
    real, demo = _unit("mcd-jielong.service"), _unit("mcd-jielong-demo.service")
    assert "--no-access-log" in _exec(real)
    assert "--no-access-log" not in _exec(demo)
    assert "MCD_MODE=real" in real and "MCD_DB=/var/lib/mcd-jielong/app.db" in real
    assert "AWS_REGION=ap-northeast-1" in real and "AWS_DEFAULT_REGION=ap-northeast-1" in real
    assert "ReadWritePaths=/var/lib/mcd-jielong\n" in real
    assert "MCD_MODE=demo" in demo and "MCD_DB=/var/lib/mcd-jielong-demo/app.db" in demo
    assert "MCD_PUBLIC_BASE_URL=https://mcd.aws.panlm.click/demo" in demo
    assert "ReadWritePaths=/var/lib/mcd-jielong-demo\n" in demo


CSP = ("default-src 'self'; img-src 'self' https://menu-img.mcd.cn data:; script-src 'self'; "
       "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


def test_caddyfile_sets_csp():
    t = (D / "Caddyfile").read_text()
    assert f'Content-Security-Policy "{CSP}"' in t
    hdr = t[t.index("header {"):]
    assert hdr.index("Content-Security-Policy") < hdr.index("}")  # inside the header block


def test_web_needs_no_inline_script_or_style():
    """The CSP above has no 'unsafe-inline': pages must load script/style from files only.
    (Setting element.style.* from JS is CSSOM and is not affected by style-src.)"""
    web = D.parent / "web"
    for f in web.glob("*.html"):
        h = f.read_text(encoding="utf-8")
        assert "<style" not in h.lower(), f.name
        assert not re.search(r"\sstyle\s*=", h, re.I), f.name
        assert not re.search(r"\son[a-z]+\s*=", h, re.I), f.name
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", h, re.I | re.S):
            assert "src=" in m.group(1) and not m.group(2).strip(), f.name
        assert "javascript:" not in h.lower(), f.name
    for f in web.glob("*.js"):
        js = f.read_text(encoding="utf-8")
        for bad in ("setAttribute('style'", 'setAttribute("style"', "cssText", "eval(",
                    "new Function", "javascript:", "document.write"):
            assert bad not in js, (f.name, bad)


def test_caddyfile():
    t = (D / "Caddyfile").read_text()
    assert not re.search(r"^\s*log\b", t, re.M)
    assert "disable_http_challenge" in t and "issuer acme" in t
    assert re.search(r"handle_path /demo/\*\s*\{\s*reverse_proxy 127\.0\.0\.1:8001", t)
    assert re.search(r"handle\s*\{\s*reverse_proxy 127\.0\.0\.1:8000", t)
    assert "redir /demo /demo/ 308" in t
    assert "nosniff" in t


def test_deploy_sh():
    p = D / "deploy.sh"
    t = p.read_text()
    assert "set -euo pipefail" in t
    assert "git status --porcelain" in t and "dirty" in t.lower()
    assert "git archive" in t and "mv -T" in t
    assert "--workers" not in t
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_deploy_sh_ordering_and_rollback():
    t = (D / "deploy.sh").read_text()
    val = t.index('caddy validate --config "$REL/deploy/Caddyfile"')
    swap = t.index('mv -T "$BASE/current.new" "$BASE/current"', val)
    inst = t.index('install -m 644 "$REL/deploy/Caddyfile"')
    assert val < swap and val < inst
    # rollback references PREV and restores state; prune only after success
    assert 'PREV=$(readlink -f "$BASE/current"' in t
    assert "ROLLED BACK" in t and "rollback()" in t
    assert t.index("ARMED=0\nfor d in") > t.index("health check FAILED")
    assert '"$d" != "$PREV"' in t
    # never rm -rf the release dir itself
    assert 'rm -rf "$REL"' not in t
    assert "curl -fsS -m 2" in t and "--no-cache-dir" in t
    assert "install -d -o mcdjl -g mcdjl -m 750 /var/lib/mcd-jielong\n" in t


def test_deploy_sh_users_dirs_and_permissions():
    """I3: mcdjl must exist; mcdjl-demo is created if missing; each data dir belongs to its own
    user; code and venv are readable by both users and writable by neither service."""
    t = (D / "deploy.sh").read_text()
    remote = t[t.index("<<'REMOTE'"):]
    assert 'id mcdjl >/dev/null 2>&1 || { echo "FATAL: system user mcdjl does not exist"' in remote
    assert re.search(r"id mcdjl-demo >/dev/null 2>&1; then\n\s*useradd --system --user-group "
                     r"--no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin mcdjl-demo",
                     remote)
    assert "install -d -o mcdjl-demo -g mcdjl-demo -m 750 /var/lib/mcd-jielong-demo" in remote
    assert "chown -R mcdjl-demo:mcdjl-demo /var/lib/mcd-jielong-demo" in remote
    assert "install -d -o mcdjl -g mcdjl -m 750 /var/lib/mcd-jielong-demo" not in remote
    # release: root-owned, world-readable (both users read it, neither can modify it)
    assert 'chown -R root:root "$REL"' in remote and 'chmod -R u=rwX,go=rX "$REL"' in remote
    assert "chown -R mcdjl:mcdjl \"$TMPREL\"" not in remote
    # venv: pip runs as mcdjl, then everything is made readable/traversable for mcdjl-demo
    pip = remote.index('runuser -u mcdjl -- "$BASE/venv/bin/pip" install')
    assert remote.index('chmod -R go+rX "$BASE/venv"') > pip
    # rollback hands the demo dir back to whichever user the restored demo unit runs as
    rb = remote[remote.index("rollback() {"):remote.index("ARMED=1")]
    assert "sed -n 's/^User=//p' /etc/systemd/system/mcd-jielong-demo.service" in rb
    assert 'chown -R "$DEMO_USER:" /var/lib/mcd-jielong-demo' in rb
    # the user exists before anything else; the demo dir changes hands only inside the rollback
    # window (after ARMED=1), right before the restart
    armed = remote.index("ARMED=1")
    own = remote.index("install -d -o mcdjl-demo -g mcdjl-demo -m 750 /var/lib/mcd-jielong-demo")
    assert remote.index("useradd") < remote.index("PREV=$(readlink") < armed < own
    assert own < remote.index("chown -R mcdjl-demo:mcdjl-demo") < remote.index(
        "systemctl restart mcd-jielong-demo", armed)


def test_remote_script_parses():
    t = (D / "deploy.sh").read_text()
    m = re.search(r"<<'REMOTE'\n(.*?)\nREMOTE\n", t, re.S)
    assert m
    r = subprocess.run(["bash", "-n"], input=m.group(1), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------- I1: no hardcoded target; env / deploy.env only ----------------

FAKE_INSTANCE = "i-" + "0" * 17          # assembled: keeps this file clear of the leak patterns
FAKE_BUCKET = "fake-release-bucket"


def _run_deploy(tmp_path, env_file=None, **env):
    """Source deploy/target.sh and run load_deploy_target in a scrubbed environment (never
    deploy.sh itself). A fake `aws` first on PATH records any call; none may happen."""
    import os
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    marker = tmp_path / "aws-called"
    fake = bindir / "aws"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> '{marker}'\nexit 99\n")
    fake.chmod(0o755)
    e = {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path),
         "DEPLOY_ENV_FILE": str(env_file or tmp_path / "absent.env")}
    e.update(env)
    script = ('set -euo pipefail; . deploy/target.sh; load_deploy_target; '
              'echo "INSTANCE_ID=$INSTANCE_ID REGION=$REGION BUCKET=$BUCKET"')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=e, cwd=str(D.parent), timeout=30)
    return r, marker


def test_deploy_sh_has_no_hardcoded_target():
    t = (D / "deploy.sh").read_text()
    lib = (D / "target.sh").read_text()
    for text in (t, lib):
        assert not re.search(r"^\s*(INSTANCE_ID|BUCKET|REGION)=[^$\s]", text, re.M)
        assert not re.search(r"i-0[0-9a-f]{8,}", text) and not re.search(r"(?<!\d)\d{12}(?!\d)", text)
    assert "deploy/deploy.env" in lib and "deploy.env.example" in lib
    # deploy.sh loads and validates the target before touching git or AWS
    load = t.index(". deploy/target.sh\nload_deploy_target\n")
    assert load < t.index("git status --porcelain") < t.index("aws s3 cp")
    assert '"${1:-}" = "--check-config"' in t
    r = subprocess.run(["bash", "-n", str(D / "target.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    ex = (D / "deploy.env.example").read_text()
    for k in ("INSTANCE_ID=", "REGION=", "BUCKET="):
        assert k in ex


def test_deploy_sh_fails_clearly_when_target_unset(tmp_path):
    r, marker = _run_deploy(tmp_path)
    assert r.returncode == 1 and r.stdout == ""
    assert "not set: INSTANCE_ID REGION BUCKET" in r.stderr and "deploy.env.example" in r.stderr
    assert not marker.exists()
    r, _ = _run_deploy(tmp_path, INSTANCE_ID=FAKE_INSTANCE, REGION="ap-northeast-1")
    assert r.returncode == 1 and "not set: BUCKET" in r.stderr


def test_deploy_sh_reads_env_file_and_env_wins(tmp_path):
    f = tmp_path / "deploy.env"
    f.write_text(f"# comment\n\nINSTANCE_ID={FAKE_INSTANCE}\nREGION=\"us-west-2\"\nBUCKET={FAKE_BUCKET}\r\n")
    r, marker = _run_deploy(tmp_path, env_file=f)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"INSTANCE_ID={FAKE_INSTANCE} REGION=us-west-2 BUCKET={FAKE_BUCKET}"
    r, _ = _run_deploy(tmp_path, env_file=f, REGION="eu-central-1")
    assert "REGION=eu-central-1" in r.stdout
    assert not marker.exists()


@pytest.mark.parametrize("key,val", [
    ("INSTANCE_ID", "i-0abc; rm -rf /"), ("INSTANCE_ID", "x"), ("REGION", "ap-northeast-1 x"),
    ("REGION", "$(id)"), ("BUCKET", "Bad_Bucket"), ("BUCKET", "b`id`b")])
def test_deploy_sh_rejects_malformed_target(tmp_path, key, val):
    env = {"INSTANCE_ID": FAKE_INSTANCE, "REGION": "ap-northeast-1", "BUCKET": FAKE_BUCKET, key: val}
    r, marker = _run_deploy(tmp_path, **env)
    assert r.returncode == 1 and f"FATAL: {key} is not" in r.stderr
    assert not marker.exists()


def test_deploy_env_file_unknown_key_rejected(tmp_path):
    f = tmp_path / "deploy.env"
    f.write_text("PATH=/tmp\n")
    r, _ = _run_deploy(tmp_path, env_file=f)
    assert r.returncode == 1 and "unknown key 'PATH'" in r.stderr
