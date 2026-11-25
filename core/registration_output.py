"""Keep delivery credentials within one registration call, never in job history."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from config import push_to_api as _cfg

logger = logging.getLogger(__name__)


@dataclass
class RegistrationOutput:
    ephemeral: bool
    account: dict = field(default_factory=dict)


_CURRENT: ContextVar[RegistrationOutput | None] = ContextVar("registration_output", default=None)


def ephemeral_registration() -> bool:
    current = _CURRENT.get()
    return bool(current and current.ephemeral)


@contextmanager
def registration_output():
    output = RegistrationOutput(bool(_cfg.REGISTER_PUSH_ONLY))
    marker = _CURRENT.set(output)
    try:
        yield output
    finally:
        output.account.clear()
        _CURRENT.reset(marker)


def capture_account(*, email, access_token, email_source, proxy_used) -> bool:
    output = _CURRENT.get()
    if output is None:
        return False
    output.account = dict(email=email, access_token=access_token,
                          email_source=email_source, proxy_used=proxy_used)
    return output.ephemeral


def deliver_registration(result: dict, output: RegistrationOutput) -> dict:
    """Deliver after the browser closes; preserve registration and push outcomes separately."""
    from core.push_to_api import push_account
    from core.email_provider import release_email, resolve_email_source

    account = output.account
    token = account.get("access_token") or result.get("access_token")
    email = account.get("email") or result.get("email")
    if not token or not email:
        return result
    source = account.get("email_source") or resolve_email_source(email)
    result["registration_success"] = True
    try:
        push = push_account(
            email=email, access_token=token,
            proxy_used=account.get("proxy_used"),
            source_type="outlook_external" if source == "remail" else "web",
        )
    except Exception:
        push = {"ok": False, "status": "failed", "message": "推送发生异常"}
        logger.warning("[Push] 推送发生异常")
    result["push"] = push
    logger.info("[Push] %s: %s (%s)", email, push["status"], push["message"])
    if output.ephemeral:
        result["success"] = push.get("status") == "success" and bool(push.get("ok"))
        result["error"] = None if result["success"] else f"注册成功，推送失败: {push['message']}"
        # Consume the mailbox even when delivery fails: the remote account exists.
        try:
            release_email(email, status="used", note="注册成功，已完成推送尝试")
        except Exception:
            logger.warning("[Push] 邮箱结算失败，重启后按任务记录恢复")
        for key in ("access_token", "totp_secret", "codex", "_post_actions"):
            result.pop(key, None)
        result["account_id"] = None
    return result
