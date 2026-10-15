#!/usr/bin/env bash
# Deploy HEAD to the EC2 via S3 + SSM. Run from repo root: AWS_PROFILE=<profile> deploy/deploy.sh
#
# The target is never hardcoded. It comes from the environment, or from deploy/deploy.env
# (gitignored; copy deploy/deploy.env.example). Environment variables win over the file.
#   INSTANCE_ID  EC2 instance id     REGION  AWS region     BUCKET  S3 bucket for release tarballs
# `deploy/deploy.sh --check-config` prints the resolved target and exits (no git, no AWS).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
# shellcheck source=deploy/target.sh
. deploy/target.sh
load_deploy_target

if [ "${1:-}" = "--check-config" ]; then
  echo "INSTANCE_ID=$INSTANCE_ID REGION=$REGION BUCKET=$BUCKET"
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "refusing to deploy: dirty git tree" >&2
  exit 1
fi

SHA=$(git rev-parse --short HEAD)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

git archive --format=tar.gz HEAD > "$TMP/$SHA.tgz"
aws s3 cp "$TMP/$SHA.tgz" "s3://$BUCKET/releases/$SHA.tgz" --region "$REGION" --only-show-errors

cat > "$TMP/remote.sh" <<REMOTE
set -euo pipefail
SHA=$SHA
BUCKET=$BUCKET
REGION=$REGION
REMOTE
cat >> "$TMP/remote.sh" <<'REMOTE'
BASE=/opt/mcd-jielong
REL=$BASE/releases/$SHA
CADDYFILE=/etc/caddy/Caddyfile
UNITS="mcd-jielong.service mcd-jielong-demo.service"

id mcdjl >/dev/null 2>&1 || { echo "FATAL: system user mcdjl does not exist" >&2; exit 1; }
# The public demo runs as its own user so it cannot read the real instance's data (I3).
if ! id mcdjl-demo >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin mcdjl-demo
fi
id mcdjl-demo >/dev/null 2>&1 || { echo "FATAL: system user mcdjl-demo does not exist" >&2; exit 1; }
install -d -o mcdjl -g mcdjl -m 750 /var/lib/mcd-jielong
mkdir -p "$BASE/releases"
chmod 755 "$BASE" "$BASE/releases"

# Extract into a temp dir and move into place; never delete an existing release
# (it may be the one `current` points to).
if [ -d "$REL" ]; then
  echo "release $SHA already present, reusing"
else
  /snap/bin/aws s3 cp "s3://$BUCKET/releases/$SHA.tgz" "/tmp/$SHA.tgz" --region "$REGION" --only-show-errors
  TMPREL="$REL.tmp.$$"
  rm -rf "$TMPREL"; mkdir -p "$TMPREL"
  tar -xzf "/tmp/$SHA.tgz" -C "$TMPREL"
  rm -f "/tmp/$SHA.tgz"
  mv -T "$TMPREL" "$REL"
fi
# Code is root-owned and world-readable: both service users (mcdjl, mcdjl-demo) can read it,
# neither can change it. Also normalizes releases deployed before I3 (owned by mcdjl).
chown -R root:root "$REL"
chmod -R u=rwX,go=rX "$REL"

if [ ! -x "$BASE/venv/bin/python" ]; then
  python3 -m venv "$BASE/venv"
  chown -R mcdjl:mcdjl "$BASE/venv"
fi
HOME="$BASE" runuser -u mcdjl -- "$BASE/venv/bin/pip" install --quiet --no-cache-dir -r "$REL/requirements.txt"
# Both service users run this interpreter: make every file readable, dirs/executables traversable.
chmod -R go+rX "$BASE/venv"

# Validate the NEW Caddyfile from the release dir before touching anything live.
caddy validate --config "$REL/deploy/Caddyfile" --adapter caddyfile

# Snapshot current live state for rollback.
PREV=$(readlink -f "$BASE/current" || true)
BK=$(mktemp -d)
for u in $UNITS; do
  if [ -e "/etc/systemd/system/$u" ]; then cp -p "/etc/systemd/system/$u" "$BK/$u"; fi
done
if [ -e "$CADDYFILE" ]; then cp -p "$CADDYFILE" "$BK/Caddyfile"; cp -p "$CADDYFILE" "$CADDYFILE.prev"; fi

rollback() {
  ARMED=0
  set +e
  echo "deploy failed, rolling back" >&2
  if [ -n "$PREV" ] && [ -d "$PREV" ]; then
    ln -sfn "$PREV" "$BASE/current.new" && mv -T "$BASE/current.new" "$BASE/current"
  fi
  for u in $UNITS; do
    if [ -e "$BK/$u" ]; then cp -p "$BK/$u" "/etc/systemd/system/$u"; else rm -f "/etc/systemd/system/$u"; fi
  done
  # the restored demo unit may run as another user (before I3: mcdjl); give it its DB dir back
  DEMO_USER=$(sed -n 's/^User=//p' /etc/systemd/system/mcd-jielong-demo.service 2>/dev/null | head -n 1)
  if [ -n "$DEMO_USER" ] && id "$DEMO_USER" >/dev/null 2>&1; then
    chown -R "$DEMO_USER:" /var/lib/mcd-jielong-demo
  fi
  if [ -e "$BK/Caddyfile" ]; then cp -p "$BK/Caddyfile" "$CADDYFILE"; fi
  systemctl daemon-reload
  if [ -n "$PREV" ] && [ -d "$PREV" ]; then
    systemctl restart mcd-jielong
    systemctl restart mcd-jielong-demo
  else
    systemctl stop mcd-jielong mcd-jielong-demo
  fi
  systemctl reload caddy
  echo "ROLLED BACK to ${PREV:-<nothing: no previous release>}" >&2
}
ARMED=1
trap 'rc=$?; if [ "${ARMED:-0}" = 1 ] && [ "$rc" -ne 0 ]; then rollback; fi; exit "$rc"' EXIT

# Switch over.
ln -sfn "$REL" "$BASE/current.new"
mv -T "$BASE/current.new" "$BASE/current"
for u in $UNITS; do install -m 644 "$REL/deploy/$u" "/etc/systemd/system/$u"; done
install -m 644 "$REL/deploy/Caddyfile" "$CADDYFILE"
caddy validate --config "$CADDYFILE" --adapter caddyfile
# The new demo unit runs as mcdjl-demo. Done only here, inside the rollback window: the old
# demo (before I3: mcdjl) keeps a writable dir until it is restarted, and rollback() hands the
# dir back to whatever user the restored unit names. The demo DB is mock data only.
install -d -o mcdjl-demo -g mcdjl-demo -m 750 /var/lib/mcd-jielong-demo
chown -R mcdjl-demo:mcdjl-demo /var/lib/mcd-jielong-demo
systemctl daemon-reload
systemctl enable mcd-jielong mcd-jielong-demo
# restart = stop then start: exactly one process per DB at all times
systemctl restart mcd-jielong
systemctl restart mcd-jielong-demo
systemctl reload caddy

ok=0
for i in $(seq 1 20); do
  if curl -fsS -m 2 http://127.0.0.1:8000/api/meta >/dev/null 2>&1 && curl -fsS -m 2 http://127.0.0.1:8001/api/meta >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
echo "mcd-jielong: $(systemctl is-active mcd-jielong || true)"
echo "mcd-jielong-demo: $(systemctl is-active mcd-jielong-demo || true)"
if [ "$ok" != 1 ]; then echo "health check FAILED" >&2; exit 1; fi

# Healthy: disarm rollback, prune old releases (never PREV or the new one).
ARMED=0
for d in $(ls -1dt "$BASE"/releases/*/ | tail -n +4); do
  d=${d%/}
  if [ "$d" != "$REL" ] && [ "$d" != "$PREV" ]; then rm -rf "$d"; fi
done
rm -rf "$BK"
echo "deployed $SHA"
REMOTE

B64=$(base64 < "$TMP/remote.sh" | tr -d '\n')
python3 -I - "$TMP/params.json" "$B64" <<'PY'
import json, sys
path, b64 = sys.argv[1], sys.argv[2]
cmd = f"echo {b64} | base64 -d | bash"
json.dump({"commands": [cmd]}, open(path, "w"))
PY

CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript --comment "mcd-jielong deploy $SHA" \
  --parameters "file://$TMP/params.json" --query Command.CommandId --output text)
echo "command: $CMD_ID"

STATUS=InProgress
for _ in $(seq 1 180); do
  sleep 4
  STATUS=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in Success|Failed|Cancelled|TimedOut|Cancelling) break ;; esac
done

echo "status: $STATUS"
case "$STATUS" in Success|Failed|Cancelled|TimedOut) ;; *) echo "WARNING: polling timed out after ~12 min; the remote run may still be in progress (command $CMD_ID)" >&2 ;; esac
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
  --query StandardOutputContent --output text | tail -n 30 || true
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
  --query StandardErrorContent --output text | tail -n 30 >&2 || true
[ "$STATUS" = Success ]
