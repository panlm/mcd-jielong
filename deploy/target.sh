# Sourced by deploy/deploy.sh (and by the tests). Defines load_deploy_target, which sets
# INSTANCE_ID, REGION and BUCKET from the environment or from an env file, validates them,
# and exits with a clear message when one is missing or malformed. Nothing is hardcoded.
#
# Env file: $DEPLOY_ENV_FILE, default deploy/deploy.env (gitignored; copy
# deploy/deploy.env.example). KEY=VALUE lines only; it is parsed, never executed.
# Environment variables win over the file.

load_deploy_target() {
  local env_file=${DEPLOY_ENV_FILE:-deploy/deploy.env} line key val v missing=""
  if [ -f "$env_file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      line=${line%$'\r'}
      case "$line" in ''|'#'*) continue ;; esac
      key=${line%%=*}
      val=${line#*=}
      case "$key" in
        INSTANCE_ID|REGION|BUCKET) ;;
        *) echo "FATAL: $env_file: unknown key '$key' (allowed: INSTANCE_ID REGION BUCKET)" >&2; exit 1 ;;
      esac
      val=${val#\"}; val=${val%\"}; val=${val#\'}; val=${val%\'}
      if [ -z "${!key:-}" ]; then printf -v "$key" '%s' "$val"; fi
    done < "$env_file"
  fi
  for v in INSTANCE_ID REGION BUCKET; do
    if [ -z "${!v:-}" ]; then missing="$missing $v"; fi
  done
  if [ -n "$missing" ]; then
    echo "FATAL: not set:$missing. Export them or put them in $env_file (see deploy/deploy.env.example)." >&2
    exit 1
  fi
  # validated before use: deploy.sh interpolates these into the remote script
  [[ $INSTANCE_ID =~ ^i-[0-9a-f]{8,17}$ ]] || { echo "FATAL: INSTANCE_ID is not an EC2 instance id" >&2; exit 1; }
  [[ $REGION =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || { echo "FATAL: REGION is not an AWS region name" >&2; exit 1; }
  [[ $BUCKET =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || { echo "FATAL: BUCKET is not an S3 bucket name" >&2; exit 1; }
}
