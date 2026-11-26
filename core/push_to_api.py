"""Push registered accounts to a configured HTTP endpoint."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import requests

from config import push_to_api as _cfg

logger = logging.getLogger(__name__)


def validate_push_config() -> str:
    url = str(getattr(_cfg, "PUSH_API_URL", "") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("请先配置有效的 PUSH_API_URL，再开始注册")
    return url


def push_account(
    *,
    email: str,
    access_token: str,
    proxy_used: str | None = None,
    source_type: str = "web",
) -> dict[str, Any]:
    url = str(getattr(_cfg, "PUSH_API_URL", "") or "").strip()
    if not url:
        message = "PUSH_API_URL is not configured"
        return {"ok": True, "status": "skipped", "message": message}
    if not access_token:
        message = "access_token is empty"
        return {"ok": False, "status": "failed", "message": message}
    try:
        url = validate_push_config()
    except ValueError as exc:
        return {"ok": False, "status": "failed", "message": str(exc)}

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = str(getattr(_cfg, "PUSH_API_TOKEN", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # The chatgpt2api account import endpoint accepts a batch-shaped payload.
    # Keep accepting either a base URL or a full /api/accounts URL in panel
    # configuration so existing instances can be migrated without rewriting it.
    parsed = urlsplit(url)
    api_url = url.rstrip("/")
    if not parsed.path.rstrip("/").endswith("/api/accounts"):
        api_url += "/api/accounts"

    account = {
        "email": email,
        "access_token": access_token,
        "type": "free",
        "source_type": str(source_type or "web").strip() or "web",
    }
    if proxy_used and _cfg.PUSH_API_INCLUDE_PROXY:
        account["proxy"] = proxy_used
    payload = {"accounts": [account]}
    try:
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=max(1, int(getattr(_cfg, "PUSH_API_TIMEOUT", 15))),
            verify=bool(getattr(_cfg, "PUSH_API_VERIFY_TLS", True)),
            allow_redirects=False,
        )
        if 200 <= response.status_code < 300:
            try:
                body = response.json()
            except Exception:
                return {"ok": False, "status": "failed", "message": "接收端未返回导入结果"}
            if not isinstance(body, dict):
                return {"ok": False, "status": "failed", "message": "接收端未返回导入结果"}
            if not any(key in body for key in ("added", "refreshed", "skipped", "ok", "success")):
                return {"ok": False, "status": "failed", "message": "接收端未返回导入结果"}
            failures = body.get("errors") or body.get("error")
            if failures or body.get("ok") is False or body.get("success") is False:
                return {"ok": False, "status": "failed", "message": "接收端拒绝导入账号"}
            added = body.get("added")
            if added is not None and not any(body.get(key) for key in ("added", "refreshed", "skipped")):
                return {"ok": False, "status": "failed", "message": "接收端未导入账号"}
            return {
                "ok": True,
                "status": "success",
                "message": f"added={added}" if added is not None else f"HTTP {response.status_code}",
            }
        message = f"HTTP {response.status_code}"
    except Exception as exc:
        # Receiver bodies and exception URLs may echo tokens or credentials.
        message = f"推送请求失败: {type(exc).__name__}"

    message = message[:500]
    return {"ok": False, "status": "failed", "message": message}
