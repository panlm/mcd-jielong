"""Runtime settings."""
import os
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
DEMO_RETENTION_S = 2 * 3600
REAL_RETENTION_S = 7 * 24 * 3600


@dataclass
class Settings:
    mode: Literal["real", "demo"]
    db_path: str
    public_base_url: str
    host_key: str | None = None
    retention_s: int = DEMO_RETENTION_S
    tz: ZoneInfo = TZ


SSM_REGION = "ap-northeast-1"
SSM_TOKEN = "/mcd-1024/mcd-mcp-token"  # nosec B105 - SSM parameter name, not a secret
SSM_HOST_KEY = "/mcd-1024/host-key"  # nosec B105 - SSM parameter name, not a secret


def load_settings() -> Settings:
    mode = os.environ.get("MCD_MODE", "")
    if mode not in ("real", "demo"):
        raise RuntimeError(f"MCD_MODE must be 'real' or 'demo', got {mode!r}")
    default_db = "./data/demo.db" if mode == "demo" else "./data/real.db"
    return Settings(
        mode=mode,
        db_path=os.environ.get("MCD_DB", default_db),
        public_base_url=os.environ.get("MCD_PUBLIC_BASE_URL", "http://127.0.0.1:8001").rstrip("/"),
        retention_s=DEMO_RETENTION_S if mode == "demo" else REAL_RETENTION_S,
    )


def load_secrets(settings: Settings) -> tuple[str, str]:
    """(mcp_token, host_key) for real mode. Env/file first, SSM as fallback. Never logged."""
    token_file = os.environ.get("MCD_TOKEN_FILE")
    env_key = os.environ.get("MCD_HOST_KEY")
    token = host_key = None
    if token_file is not None:  # explicitly configured: use it as-is, no SSM fallback
        with open(token_file, encoding="utf-8") as f:
            token = f.read().strip()
    if env_key is not None:
        host_key = env_key.strip()
    if token is None or host_key is None:
        import boto3  # lazy: demo path must never import it

        ssm = boto3.client("ssm", region_name=SSM_REGION)

        def get(name: str) -> str:
            return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].strip()

        token = get(SSM_TOKEN) if token is None else token
        host_key = get(SSM_HOST_KEY) if host_key is None else host_key
    if not token or not host_key:
        raise RuntimeError("real mode requires a non-empty MCP token and host key")
    return token, host_key
