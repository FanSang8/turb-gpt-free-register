"""Durable Remail purchase, mailbox-slot and reservation coordinator."""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests
from core.diagnostics import diagnostic_span, safe_error, traced

from config import remail as _cfg
from core.remail_client import (
    RemailAmbiguousPurchaseError,
    RemailClient,
    RemailError,
    RemailProduct,
    decimal_value,
    _base_url,
)

logger = logging.getLogger(__name__)

_TERMINAL_SLOT_STATES = {"succeeded", "failed", "invalid"}
_ACTIVE_JOB_STATES = {"pending", "running", "stopping"}
_WEB_EDITABLE_SETTINGS = frozenset(
    {
        "REMAIL_ENABLED",
        "REMAIL_BASE_URL",
        "REMAIL_PROJECT_ID",
        "REMAIL_PRODUCT_ID",
        "REMAIL_EMAIL_SUFFIX",
        "REMAIL_EXPECTED_UNIT_POINTS",
        "REMAIL_REQUEST_TIMEOUT_SECONDS",
        "REMAIL_PURCHASE_BATCH_SIZE",
        "REMAIL_MAX_PURCHASE_PER_ROUND",
        "REMAIL_LOW_BALANCE_POINTS",
        "REMAIL_LOW_BALANCE_ALERT_INTERVAL_SECONDS",
    }
)


def _setting(name: str) -> Any:
    # Older bind-mounted instance configs predate REMAIL_PRODUCT_ID.  Treat a
    # missing value as unset so they can be upgraded without losing data.
    compatibility_defaults = {
        "REMAIL_PRODUCT_ID": 0,
        "REMAIL_PURCHASE_BATCH_SIZE": 4,
    }
    default = getattr(_cfg, name, compatibility_defaults.get(name))
    if default is None:
        raise AttributeError(f"config.remail has no setting {name}")
    raw = os.getenv(f"REGISTER_{name}")
    # Empty values in .env.example are placeholders, not overrides.  Treat
    # them as unset so WebUI-managed values remain editable at runtime.
    if raw is None or not str(raw).strip():
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


def apply_runtime_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Apply WebUI Remail values directly to the module used by this process.

    ``importlib.reload`` is normally enough, but bind-mounted configuration can
    retain a stale module value on some deployments.  Applying the already
    validated WebUI payload to the exact module object held by this manager
    makes the save operation deterministic.  Environment overrides are still
    detected and reported instead of showing a false "hot reload succeeded".
    """
    applied: dict[str, Any] = {}
    for name, raw in updates.items():
        if name not in _WEB_EDITABLE_SETTINGS:
            continue
        current = getattr(_cfg, name, 0 if name == "REMAIL_PRODUCT_ID" else None)
        if current is None:
            raise AttributeError(f"config.remail has no setting {name}")
        if isinstance(current, bool):
            value = (
                raw.strip().lower() in {"1", "true", "yes", "on"}
                if isinstance(raw, str)
                else bool(raw)
            )
        elif isinstance(current, int):
            value = int(raw)
        elif isinstance(current, float):
            value = float(raw)
        else:
            value = str(raw)
        setattr(_cfg, name, value)
        effective = _setting(name)
        if effective != value:
            raise RuntimeError(
                f"{name} is overridden by environment variable REGISTER_{name}"
            )
        applied[name] = effective
    # A circuit-open instance is intentionally disabled at runtime.  Turning
    # Remail back on from the WebUI is the explicit operator acknowledgement:
    # clear the persisted circuit at that point so the next reconcile can try
    # again.  Do this only after all environment-override checks above pass.
    if applied.get("REMAIL_ENABLED") is True:
        manager = globals().get("remail_manager")
        if manager is not None:
            manager.reset_circuit()
    return applied


def _runtime_circuit_open() -> bool:
    """Return the persisted circuit state without making a Remail request."""
    manager = globals().get("remail_manager")
    if manager is None:
        return False
    try:
        return manager.circuit_open()
    except Exception:
        logger.debug("Failed to read Remail circuit state", exc_info=True)
        return False


def enabled() -> bool:
    return bool(_setting("REMAIL_ENABLED")) and not _runtime_circuit_open()


def configured() -> bool:
    return bool(enabled() and str(_setting("REMAIL_API_KEY") or "").strip())


def _now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).astimezone().isoformat(timespec="seconds")


def _instance_label() -> str:
    explicit = str(os.getenv("REGISTER_INSTANCE_LABEL") or "").strip()
    if not explicit:
        try:
            from config import push_to_api

            explicit = str(getattr(push_to_api, "PUSH_INSTANCE_LABEL", "") or "").strip()
        except Exception:
            explicit = ""
    if explicit:
        return explicit
    for index, arg in enumerate(sys.argv):
        if arg == "--port" and index + 1 < len(sys.argv):
            return f"register-{sys.argv[index + 1]}"
        if arg.startswith("--port="):
            return f"register-{arg.partition('=')[2]}"
    return "register"


def _parse_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _default_state() -> dict:
    return {
        "version": 1,
        "orders": [],
        "purchase_intents": [],
        "circuit": {"open": False, "reason": "", "opened_at": None},
        "alerts": {
            "low_balance_active": False,
            "last_low_balance_at": None,
            "last_recovery_at": None,
        },
        "balance_points": None,
        "last_balance_at": None,
        "last_purchase_at": None,
        "last_purchase_error": "",
    }


def _find_decimal(obj: Any, keys: tuple[str, ...]) -> Decimal | None:
    if isinstance(obj, dict):
        for key in keys:
            if key in obj:
                value = decimal_value(obj.get(key))
                if value is not None:
                    return value
        for value in obj.values():
            found = _find_decimal(value, keys)
            if found is not None:
                return found
    return None


class RemailManager:
    def __init__(
        self,
        *,
        state_path: str | Path | None = None,
        client_factory: Callable[[], RemailClient] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        root = Path(os.getenv("REGISTER_DATA_DIR") or Path(__file__).resolve().parent.parent)
        self.state_path = Path(state_path) if state_path else root / "remail_state.json"
        self._client_factory = client_factory
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleeper
        self._lock = threading.RLock()
        self._snapshot_lock = threading.Lock()
        self._circuit_snapshot: tuple[tuple[int, int, int] | None, bool] | None = None
        self._context_lock = threading.Lock()
        self._context_cache: dict[str, dict] = {}
        self._recovered = False
        self._recovery_start_lock = threading.Lock()
        self._recovery_started = False

    def _client(self) -> RemailClient:
        if self._client_factory:
            return self._client_factory()
        return RemailClient(
            _base_url(str(_setting("REMAIL_BASE_URL"))),
            str(_setting("REMAIL_API_KEY")),
            float(_setting("REMAIL_REQUEST_TIMEOUT_SECONDS")),
        )

    def _load(self) -> dict:
        if not self.state_path.exists():
            return _default_state()
        try:
            started = time.monotonic()
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            elapsed = time.monotonic() - started
            if elapsed >= 1:
                logger.warning(
                    "event=remail_state_load elapsed_ms=%d state_bytes=%d thread_id=%d",
                    round(elapsed * 1000), self.state_path.stat().st_size,
                    threading.get_ident(),
                )
        except Exception:
            logger.exception("[Remail] failed to read state; using an empty state")
            return _default_state()
        if not isinstance(state, dict):
            return _default_state()
        base = _default_state()
        base.update(state)
        if not isinstance(base.get("orders"), list):
            base["orders"] = []
        if not isinstance(base.get("purchase_intents"), list):
            base["purchase_intents"] = []
        if not isinstance(base.get("circuit"), dict):
            base["circuit"] = _default_state()["circuit"]
        if not isinstance(base.get("alerts"), dict):
            base["alerts"] = _default_state()["alerts"]
        return base

    def _save(self, state: dict) -> None:
        # Completed account credentials never belong here. Paid mailbox pickup
        # tokens are kept only while a main/child slot still needs them.
        for order in state.get("orders", []):
            slots = order.get("slots") or []
            if slots and all(slot.get("status") in _TERMINAL_SLOT_STATES for slot in slots):
                order.pop("service_token", None)
        started = time.monotonic()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        with self._snapshot_lock:
            tmp.replace(self.state_path)
            self._circuit_snapshot = (
                self._state_signature(),
                bool((state.get("circuit") or {}).get("open")),
            )
        elapsed = time.monotonic() - started
        if elapsed >= 1:
            logger.warning(
                "event=remail_state_save elapsed_ms=%d state_bytes=%d thread_id=%d",
                round(elapsed * 1000), self.state_path.stat().st_size,
                threading.get_ident(),
            )

    def _state_signature(self) -> tuple[int, int, int] | None:
        try:
            info = self.state_path.stat()
        except FileNotFoundError:
            return None
        return info.st_ino, info.st_mtime_ns, info.st_size

    def circuit_open(self) -> bool:
        """Read whether the persisted purchase circuit is open.

        This is deliberately a local-only read.  ``enabled()`` uses it to
        disable Remail after a circuit trip without changing the operator's
        configured ``REMAIL_ENABLED`` value.
        """
        # This lock never covers purchases. Most callers only stat the file;
        # decode it once on startup or after an external atomic replacement.
        with self._snapshot_lock:
            signature = self._state_signature()
            if self._circuit_snapshot is None or self._circuit_snapshot[0] != signature:
                self._circuit_snapshot = (
                    signature,
                    bool((self._load().get("circuit") or {}).get("open")),
                )
            return self._circuit_snapshot[1]

    @staticmethod
    def _iter_slots(state: dict):
        for order in state.get("orders", []):
            for slot in order.get("slots", []):
                yield order, slot

    def _build_context(self, order: dict, slot: dict) -> dict | None:
        if slot.get("status") in _TERMINAL_SLOT_STATES:
            return None
        token = str(order.get("service_token") or "")
        base_email = str(order.get("delivery_email") or "")
        if not token or not base_email:
            return None
        url = f"{_base_url(str(_setting('REMAIL_BASE_URL')))}/v1/pickup?{urlencode({'email': base_email, 'token': token})}"
        return {
            "email": slot["email"],
            "base_email": base_email,
            "code_url": url,
            "reservation_id": slot["reservation_id"],
            "order_no": order["order_no"],
        }

    def _cache_contexts(self, state: dict) -> None:
        contexts = {}
        for order, slot in self._iter_slots(state):
            context = self._build_context(order, slot)
            if context:
                contexts[str(slot.get("email") or "").strip().lower()] = context
        with self._context_lock:
            self._context_cache = contexts

    @staticmethod
    def _find_slot(state: dict, reservation_id: str) -> tuple[dict, dict] | tuple[None, None]:
        for order, slot in RemailManager._iter_slots(state):
            if str(slot.get("reservation_id")) == str(reservation_id):
                return order, slot
        return None, None

    @staticmethod
    def _order_no(order: dict) -> str:
        return str(order.get("orderNo") or order.get("order_no") or order.get("id") or "").strip()

    @staticmethod
    def _api_key_id_from_profile(payload: dict | None) -> int | None:
        raw = payload.get("id") if isinstance(payload, dict) else None
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _current_api_key_id(self, client: RemailClient) -> int | None:
        """Read the authenticated Key ID used to isolate recovery candidates."""
        getter = getattr(client, "get_api_key_profile", None)
        if not callable(getter):
            # Compatibility with lightweight/fake clients and old deployments.
            return None
        try:
            return self._api_key_id_from_profile(getter())
        except Exception as exc:
            logger.warning("[Remail] 读取当前 API Key ID 失败，将不按 apiKeyId 恢复: %s", exc)
            return None

    def _snapshot_order_nos(self, client: RemailClient, api_key_id: int | None) -> list[str]:
        """Capture remote orders before POST so stale orders cannot be adopted."""
        try:
            orders = client.list_orders(limit=100)
        except Exception as exc:
            logger.warning("[Remail] 下单前订单快照失败，超时恢复仍会按时间匹配: %s", exc)
            return []
        result = []
        for item in orders:
            if api_key_id is not None:
                try:
                    if int(item.get("apiKeyId")) != api_key_id:
                        continue
                except (TypeError, ValueError):
                    continue
            order_no = self._order_no(item)
            if order_no:
                result.append(order_no)
        return result

    def _open_circuit(self, state: dict, reason: str) -> None:
        state["circuit"] = {
            "open": True,
            "reason": str(reason)[:500],
            "opened_at": _now_iso(self._clock()),
        }
        state["last_purchase_error"] = str(reason)[:500]
        self._send_feishu("Remail 自动采购已熔断", str(reason)[:500], template="red")

    def reset_circuit(self) -> dict:
        with self._lock:
            state = self._load()
            state["circuit"] = {"open": False, "reason": "", "opened_at": None}
            state["last_purchase_error"] = ""
            self._save(state)
            return self.status()

    def _send_feishu(self, title: str, detail: str, *, template: str = "orange") -> bool:
        url = str(_setting("REMAIL_FEISHU_WEBHOOK_URL") or "").strip()
        if not url:
            try:
                from config import push_to_api
                url = str(getattr(push_to_api, "FEISHU_WEBHOOK_URL", "") or "").strip()
            except Exception:
                url = ""
        if not url:
            return False
        instance = _instance_label()
        icon = "✅" if template == "green" else "⚠️"
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{icon} [{instance}] {title}"},
                    "template": template,
                },
                "elements": [{
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**实例：** {instance}\n{str(detail)[:900]}",
                    },
                }],
            },
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("[Remail] Feishu notification failed: %s", exc)
            return False

    def _handle_balance(self, state: dict, balance: Decimal) -> None:
        now = self._clock()
        threshold = Decimal(str(max(0, int(_setting("REMAIL_LOW_BALANCE_POINTS")))))
        alerts = state.setdefault("alerts", {})
        state["balance_points"] = str(balance)
        state["last_balance_at"] = _now_iso(now)
        if balance <= threshold:
            last = _parse_ts(alerts.get("last_low_balance_at")) or 0.0
            interval = max(60, int(_setting("REMAIL_LOW_BALANCE_ALERT_INTERVAL_SECONDS")))
            if not alerts.get("low_balance_active") or now - last >= interval:
                self._send_feishu(
                    "Remail 余额不足提醒",
                    f"当前余额 **{balance}** 积分，已经达到或低于阈值 **{threshold}**。"
                    "当前策略为仅告警，自动采购仍会继续。",
                )
                alerts["last_low_balance_at"] = _now_iso(now)
            alerts["low_balance_active"] = True
        elif alerts.get("low_balance_active"):
            self._send_feishu(
                "Remail 余额已恢复",
                f"当前余额 **{balance}** 积分，已高于阈值 **{threshold}**。",
                template="green",
            )
            alerts["low_balance_active"] = False
            alerts["last_recovery_at"] = _now_iso(now)

    def refresh_balance(self, state: dict | None = None, client: RemailClient | None = None) -> Decimal:
        owns_state = state is None
        with self._lock:
            state = state or self._load()
            wallet = (client or self._client()).get_wallet()
            balance = _find_decimal(
                wallet,
                (
                    "consumerBalance",
                    "balance",
                    "availableBalance",
                    "availablePoints",
                    "points",
                    "credit",
                ),
            )
            if balance is None:
                raise RemailError("Remail wallet response does not contain a balance")
            self._handle_balance(state, balance)
            if owns_state:
                self._save(state)
            return balance

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _historical_product_id(self, project_id: int, suffix: str, service_mode: str) -> int | None:
        """Infer a product id only from a prior successful local order.

        Some Remail project responses omit the product id even though order
        responses persist it.  The most frequently used matching historical
        id is a safe bootstrap fallback; a fresh instance still needs the
        explicit ``REMAIL_PRODUCT_ID`` setting.
        """
        ids: list[int] = []
        state = self._load()
        for order in state.get("orders", []):
            if not isinstance(order, dict):
                continue
            try:
                if int(order.get("project_id") or 0) != project_id:
                    continue
            except (TypeError, ValueError):
                continue
            if str(order.get("service_mode") or service_mode).strip().lower() != service_mode:
                continue
            email = str(order.get("delivery_email") or "").strip().lower()
            if email and not email.endswith("@" + suffix):
                continue
            product_id = self._positive_int(order.get("product_id"))
            if product_id is not None:
                ids.append(product_id)
        return Counter(ids).most_common(1)[0][0] if ids else None

    def _select_product(self, client: RemailClient) -> RemailProduct:
        # Product price/inventory is financial state, so validate it on every
        # purchase instead of trusting the cache across reconcile calls.
        project_id = int(_setting("REMAIL_PROJECT_ID"))
        suffix = str(_setting("REMAIL_EMAIL_SUFFIX")).strip().lower()
        service_mode = str(_setting("REMAIL_SERVICE_MODE")).strip().lower()
        if project_id != 2:
            raise RemailError(f"Remail ChatGPT project must be 2, got {project_id}")
        if suffix != "outlook.com":
            raise RemailError(f"Remail email suffix must be outlook.com, got {suffix}")
        if service_mode != "purchase":
            raise RemailError(f"Remail service mode must be purchase, got {service_mode}")
        configured_product_id = self._positive_int(_setting("REMAIL_PRODUCT_ID"))
        historical_product_id = None if configured_product_id else self._historical_product_id(
            project_id, suffix, service_mode
        )
        payload = client.get_project(project_id)
        project = payload.get("project") if isinstance(payload.get("project"), dict) else payload
        actual_id = int(project.get("id") or project_id)
        if actual_id != project_id:
            raise RemailError(f"Remail project id mismatch: expected {project_id}, got {actual_id}")
        name = str(project.get("name") or "").strip()
        if name and "chatgpt" not in name.casefold():
            raise RemailError(f"Remail project {project_id} is not ChatGPT: {name}")
        products = payload.get("products") or project.get("products") or []
        matches: list[RemailProduct] = []
        expected = Decimal(str(int(_setting("REMAIL_EXPECTED_UNIT_POINTS"))))
        wrong_prices: list[str] = []
        missing_product_id = False
        for raw in products:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("type") or "").lower() != "microsoft":
                continue
            if str(raw.get("status") or "enabled").lower() != "enabled":
                continue
            if raw.get("purchaseEnabled") is False:
                continue
            suffix_item = next(
                (
                    item for item in (raw.get("suffixes") or [])
                    if isinstance(item, dict) and str(item.get("suffix") or "").lower() == suffix
                ),
                None,
            )
            if suffix_item is None:
                continue
            available_raw = suffix_item.get("publicAvailable")
            available = int(available_raw) if available_raw is not None else None
            if available is not None and available <= 0:
                continue
            price = decimal_value(raw.get("purchasePrice"))
            if price is None or price > expected:
                wrong_prices.append(str(price))
                continue
            api_product_id = next(
                (
                    self._positive_int(raw.get(key))
                    for key in ("id", "productId", "projectProductId")
                    if self._positive_int(raw.get(key)) is not None
                ),
                None,
            )
            product_id = configured_product_id or api_product_id or historical_product_id
            if product_id is None:
                missing_product_id = True
                continue
            if configured_product_id and api_product_id and configured_product_id != api_product_id:
                logger.warning(
                    "[Remail] 商品接口返回 product id=%s，但使用显式配置 REMAIL_PRODUCT_ID=%s",
                    api_product_id,
                    configured_product_id,
                )
            source = (
                "configured"
                if configured_product_id
                else "api"
                if api_product_id
                else "historical"
            )
            logger.info("[Remail] 选择 Microsoft/%s 商品 id=%s (source=%s)", suffix, product_id, source)
            matches.append(RemailProduct(project_id, product_id, price, suffix, available))
        if not matches:
            if wrong_prices:
                raise RemailError(
                    f"Remail purchase price exceeds limit; maximum {expected}, got {','.join(wrong_prices)}"
                )
            if missing_product_id:
                raise RemailError(
                    "Remail Microsoft product response has no product id; "
                    "set REMAIL_PRODUCT_ID or complete one successful order first"
                )
            raise RemailError(f"project {project_id} has no purchasable Microsoft/{suffix} product")
        if len(matches) != 1:
            raise RemailError(
                f"project {project_id} has {len(matches)} matching Microsoft/{suffix} products"
            )
        return matches[0]

    def _purchase_counts(self, state: dict) -> tuple[int, int]:
        now = self._clock()
        today = datetime.fromtimestamp(now).astimezone().date()
        hour = day = 0
        for order in state.get("orders", []):
            ts = _parse_ts(order.get("purchased_ts") or order.get("created_at"))
            if ts is None:
                continue
            if now - ts < 3600:
                hour += 1
            if datetime.fromtimestamp(ts).astimezone().date() == today:
                day += 1
        return hour, day

    def _allowed_purchase_count(self, state: dict, requested: int) -> int:
        return max(0, min(
            int(requested),
            max(0, int(_setting("REMAIL_MAX_PURCHASE_PER_ROUND"))),
        ))

    def _make_slots(self, order_no: str, email: str) -> list[dict]:
        local, domain = email.rsplit("@", 1)
        child = f"{local}+se{secrets.token_hex(4)}@{domain}"
        now = _now_iso(self._clock())
        return [
            {
                "reservation_id": f"{order_no}:main",
                "kind": "main",
                "email": email,
                "status": "available",
                "job_id": None,
                "created_at": now,
            },
            {
                "reservation_id": f"{order_no}:child:1",
                "kind": "child",
                "email": child,
                "status": "available",
                "job_id": None,
                "created_at": now,
            },
        ]

    def _adopt_order(self, state: dict, raw: dict, client: RemailClient) -> dict | None:
        order_no = self._order_no(raw)
        if not order_no:
            return None
        existing = next((item for item in state["orders"] if item.get("order_no") == order_no), None)
        if existing:
            return existing
        detail = raw
        detail_error = ""
        if not detail.get("serviceToken") or not detail.get("deliveryEmail"):
            try:
                detail = client.get_order(order_no)
            except Exception as exc:
                # The order was still paid for. Persist it so spend limits stay
                # correct, then allow an explicit sync to fill delivery data.
                detail = dict(raw)
                detail_error = f"{type(exc).__name__}: {exc}"[:500]
        email = str(detail.get("deliveryEmail") or "").strip().lower()
        token = str(detail.get("serviceToken") or "").strip()
        suffix = str(_setting("REMAIL_EMAIL_SUFFIX")).strip().lower()
        valid = bool(email.endswith("@" + suffix) and token)
        purchased_ts = _parse_ts(detail.get("createdAt")) or self._clock()
        order = {
            "order_no": order_no,
            "remote_id": detail.get("id"),
            "api_key_id": detail.get("apiKeyId"),
            "project_id": int(detail.get("projectId") or _setting("REMAIL_PROJECT_ID")),
            "product_id": int(detail.get("projectProductId") or detail.get("productId") or 0),
            "service_mode": str(detail.get("serviceMode") or _setting("REMAIL_SERVICE_MODE")),
            "client_channel": str(detail.get("clientChannel") or "api"),
            "remote_status": str(detail.get("status") or ""),
            "pay_amount": str(detail.get("payAmount") or _setting("REMAIL_EXPECTED_UNIT_POINTS")),
            "delivery_email": email,
            "service_token": token,
            "purchased_ts": purchased_ts,
            "created_at": _now_iso(purchased_ts),
            "invalid_reason": "" if valid else (
                detail_error or "missing outlook.com deliveryEmail or serviceToken"
            ),
            "slots": self._make_slots(order_no, email) if valid else [],
        }
        state["orders"].append(order)
        return order

    def _matches_recovery_order(
        self,
        raw: dict,
        product: RemailProduct,
        started_at: float,
        *,
        api_key_id: int | None = None,
        baseline_order_nos: set[str] | None = None,
    ) -> bool:
        if self._order_no(raw) == "":
            return False
        order_no = self._order_no(raw)
        if baseline_order_nos and order_no in baseline_order_nos:
            return False
        if api_key_id is not None:
            try:
                # The public API exposes apiKeyId on every order.  When the
                # field is missing, do not risk adopting another Key's order.
                if int(raw.get("apiKeyId")) != api_key_id:
                    return False
            except (TypeError, ValueError):
                return False
        created = _parse_ts(raw.get("createdAt"))
        if created is not None and created < started_at - 5:
            return False
        email = str(raw.get("deliveryEmail") or "").lower()
        raw_product_id = int(raw.get("projectProductId") or raw.get("productId") or 0)
        return (
            int(raw.get("projectId") or 0) == product.project_id
            # A zero product ID denotes the current direct suffix API, whose
            # response may omit the internally selected product ID.
            and (product.product_id <= 0 or raw_product_id == product.product_id)
            and str(raw.get("serviceMode") or "") == str(_setting("REMAIL_SERVICE_MODE"))
            and (not email or email.endswith("@" + product.email_suffix))
            and str(raw.get("clientChannel") or "api") in {"api", "api_key", "open_api"}
        )

    def _recover_ambiguous_purchase(
        self,
        state: dict,
        client: RemailClient,
        product: RemailProduct,
        quantity: int,
        started_at: float,
        *,
        api_key_id: int | None = None,
        baseline_order_nos: set[str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> list[dict]:
        timeout = max(0.0, min(30.0, float(timeout_seconds)))
        deadline = self._monotonic() + timeout
        last_candidates: list[dict] = []
        while True:
            local = {str(item.get("order_no")) for item in state["orders"]}
            candidates = [
                item for item in client.list_orders(limit=100)
                if self._order_no(item) not in local
                and self._matches_recovery_order(
                    item,
                    product,
                    started_at,
                    api_key_id=api_key_id,
                    baseline_order_nos=baseline_order_nos,
                )
            ]
            if len(candidates) == quantity:
                return candidates
            if len(candidates) > quantity:
                raise RemailError(
                    f"ambiguous purchase recovery expected at most {quantity} orders, found {len(candidates)}"
                )
            last_candidates = candidates
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                if last_candidates:
                    return last_candidates
                raise RemailError(
                    f"ambiguous purchase recovery found 0 orders within {timeout:.0f} seconds"
                )
            self._sleep(min(2.0, remaining))

    @staticmethod
    def _unresolved_intents(state: dict) -> list[dict]:
        return [
            item
            for item in state.get("purchase_intents", [])
            if item.get("status") in {"pending", "retrying", "ambiguous"}
        ]

    @staticmethod
    def _is_historical_recovery_intent(intent: dict) -> bool:
        """Failed intents from an unknown POST may still represent paid orders."""
        if str(intent.get("status") or "") != "failed":
            return False
        if intent.get("recovery_needed"):
            return True
        error = str(intent.get("error") or "").lower()
        return "purchase result is unknown" in error or "ambiguous purchase recovery" in error

    def _recover_failed_intents(
        self,
        state: dict,
        client: RemailClient,
        api_key_id: int | None,
    ) -> int:
        """Compensate old ambiguous failures without blocking new purchases.

        A retry with the original Idempotency-Key is authoritative and safe. If
        Remail still times out, do one immediate Key-scoped order scan; leave
        the intent recoverable for a later reconcile instead of marking it
        permanently resolved or purchasing a blind replacement.
        """
        recovered = 0
        now = self._clock()
        candidates = [
            item for item in state.get("purchase_intents", [])
            if self._is_historical_recovery_intent(item)
        ][-20:]
        for intent in candidates:
            last = _parse_ts(intent.get("recovery_last_at"))
            if last is not None and now - last < 30:
                continue
            expected_key_id = intent.get("api_key_id")
            if expected_key_id is not None and api_key_id is not None:
                try:
                    if int(expected_key_id) != int(api_key_id):
                        continue
                except (TypeError, ValueError):
                    continue
            intent["recovery_last_at"] = _now_iso(now)
            intent["recovery_attempts"] = int(intent.get("recovery_attempts") or 0) + 1
            if expected_key_id is None and api_key_id is not None:
                intent["api_key_id"] = int(api_key_id)
            try:
                orders, failed_items = self._submit_intent(client, intent)
            except RemailAmbiguousPurchaseError as exc:
                product = RemailProduct(
                    int(intent.get("project_id") or _setting("REMAIL_PROJECT_ID")),
                    int(intent.get("product_id") or 0),
                    Decimal(str(_setting("REMAIL_EXPECTED_UNIT_POINTS"))),
                    str(intent.get("email_suffix") or _setting("REMAIL_EMAIL_SUFFIX")),
                    None,
                )
                try:
                    orders = self._recover_ambiguous_purchase(
                        state,
                        client,
                        product,
                        int(intent.get("quantity") or 0),
                        float(intent.get("started_ts") or 0),
                        api_key_id=api_key_id,
                        baseline_order_nos=set(intent.get("baseline_order_nos") or []),
                        timeout_seconds=0,
                    )
                    failed_items = []
                except Exception as recovery_exc:
                    intent["recovery_needed"] = True
                    intent["recovery_error"] = f"{exc}; {recovery_exc}"[:500]
                    logger.warning(
                        "[Remail] 历史幂等订单仍未能确定：intent=%s: %s",
                        intent.get("intent_id"),
                        intent["recovery_error"],
                    )
                    continue
            except Exception as exc:
                intent["recovery_needed"] = True
                intent["recovery_error"] = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(
                    "[Remail] 历史幂等订单重试失败：intent=%s: %s",
                    intent.get("intent_id"),
                    intent["recovery_error"],
                )
                continue
            intent["recovery_needed"] = False
            intent["recovery_alerted"] = True
            recovered += self._finish_intent(state, client, intent, orders, failed_items)
        return recovered

    def _clear_recoverable_circuit(self, state: dict) -> bool:
        circuit = state.get("circuit") or {}
        if not circuit.get("open") or not self._unresolved_intents(state):
            return False
        reason = str(circuit.get("reason") or "").lower()
        if not any(
            hint in reason
            for hint in (
                "purchase result is unknown",
                "purchase remains retryable",
                "ambiguous purchase recovery",
                "unfinished purchase intent",
            )
        ):
            return False
        state["circuit"] = {"open": False, "reason": "", "opened_at": None}
        state["last_purchase_error"] = "Remail timeout recovery is retrying automatically"
        return True

    def _submit_intent(self, client: RemailClient, intent: dict) -> tuple[list[dict], list[dict]]:
        return client.create_orders(
            project_id=int(intent.get("project_id") or _setting("REMAIL_PROJECT_ID")),
            product_id=(int(intent["product_id"]) if int(intent.get("product_id") or 0) > 0 else None),
            email_suffix=str(intent.get("email_suffix") or _setting("REMAIL_EMAIL_SUFFIX")),
            service_mode=str(intent.get("service_mode") or _setting("REMAIL_SERVICE_MODE")),
            quantity=int(intent.get("quantity") or 0),
            idempotency_key=str(intent["intent_id"]),
            supply="public_only" if intent.get("direct_order") else None,
        )

    def _finish_intent(
        self,
        state: dict,
        client: RemailClient,
        intent: dict,
        orders: list[dict],
        failed_items: list[dict],
    ) -> int:
        adopted = []
        for raw in orders:
            try:
                order = self._adopt_order(state, raw, client)
                if order:
                    adopted.append(order)
            except Exception as exc:
                logger.warning("[Remail] failed to adopt purchased order: %s", exc)
        intent["status"] = "succeeded" if adopted else "failed"
        intent["recovery_needed"] = False
        intent["order_nos"] = [item["order_no"] for item in adopted]
        intent["completed_at"] = _now_iso(self._clock())
        intent["error"] = f"{len(failed_items)} batch item(s) failed"[:500] if failed_items else ""
        if failed_items:
            details = []
            for item in failed_items[:5]:
                if isinstance(item, dict):
                    detail = {
                        key: safe_error(item[key])
                        for key in ("index", "code", "requestId") if key in item
                    }
                    error = item.get("error") or item.get("message") or ""
                    if isinstance(error, dict):
                        error = error.get("message") or error.get("code") or "upstream error"
                    detail["error"] = safe_error(error) or "upstream supplied no error detail"
                else:
                    detail = {"error": "upstream returned a non-object failure"}
                details.append(detail)
            intent["failure_details"] = details
            intent["error"] = (
                intent["error"] + ": " + "; ".join(item["error"] for item in details)
            )[:500]
            logger.warning(
                "event=remail_purchase_failed intent=%s failed_count=%d details=%s",
                intent.get("intent_id"), len(failed_items),
                json.dumps(details, ensure_ascii=False),
            )
        state["last_purchase_at"] = _now_iso(self._clock()) if adopted else state.get("last_purchase_at")
        state["last_purchase_error"] = intent.get("error") or ""
        # Direct suffix orders use the server's current price.  The legacy
        # local price ceiling only applies to catalog-selected orders.
        if not intent.get("direct_order"):
            expected_price = Decimal(str(_setting("REMAIL_EXPECTED_UNIT_POINTS")))
            unexpected_prices = []
            for item in adopted:
                charged = decimal_value(item.get("pay_amount"))
                if charged is not None and charged > expected_price:
                    unexpected_prices.append(item.get("pay_amount"))
            if unexpected_prices:
                self._open_circuit(
                    state,
                    f"Remail charged above the configured maximum: {unexpected_prices}; maximum {expected_price}",
                )
        try:
            self.refresh_balance(state, client)
        except Exception as exc:
            logger.warning("[Remail] post-purchase balance refresh failed: %s", exc)
        if intent.get("recovery_alerted") and adopted:
            self._send_feishu(
                "Remail 下单超时已自动恢复",
                f"幂等订单已恢复，成功接管 {len(adopted)} 个邮箱订单。",
                template="green",
            )
        self._save(state)
        return len(adopted)

    def _notify_timeout(self, state: dict, intent: dict, detail: str) -> None:
        """Send useful timeout details, throttled to avoid repeated alert spam."""
        alerts = state.setdefault("alerts", {})
        now = self._clock()
        last = _parse_ts(alerts.get("last_purchase_timeout_alert_at"))
        if last is not None and now - last < 300:
            logger.warning("[Remail] 下单超时详情（通知已限流）：%s", detail)
            return
        alerts["last_purchase_timeout_alert_at"] = _now_iso(now)
        self._send_feishu(
            "Remail 下单超时，本轮已自动跳过",
            f"{detail}；本轮未熔断，后续 reconcile 会继续用同一幂等键恢复。",
        )

    def _attempt_intent(
        self,
        state: dict,
        client: RemailClient,
        intent: dict,
        *,
        api_key_id: int | None = None,
    ) -> int:
        intent["last_attempt_at"] = _now_iso(self._clock())
        try:
            with diagnostic_span(
                "remail_purchase_submit", intent_id=intent.get("intent_id"),
                quantity=intent.get("quantity"),
            ):
                orders, failed_items = self._submit_intent(client, intent)
        except RemailAmbiguousPurchaseError as first_exc:
            # The API explicitly guarantees that replaying the same
            # Idempotency-Key converges to the original order batch.  Retry
            # that authoritative path before guessing from a broad order list.
            intent["idempotency_retries"] = int(intent.get("idempotency_retries") or 0) + 1
            try:
                logger.warning(
                    "[Remail] 下单响应超时，使用相同幂等键重试：intent=%s",
                    intent.get("intent_id"),
                )
                orders, failed_items = self._submit_intent(client, intent)
            except Exception as retry_exc:
                product = RemailProduct(
                    int(intent.get("project_id") or _setting("REMAIL_PROJECT_ID")),
                    int(intent.get("product_id") or 0),
                    Decimal(str(_setting("REMAIL_EXPECTED_UNIT_POINTS"))),
                    str(intent.get("email_suffix") or _setting("REMAIL_EMAIL_SUFFIX")),
                    None,
                )
                try:
                    orders = self._recover_ambiguous_purchase(
                        state,
                        client,
                        product,
                        int(intent.get("quantity") or 0),
                        float(intent.get("started_ts") or 0),
                        api_key_id=api_key_id,
                        baseline_order_nos=set(intent.get("baseline_order_nos") or []),
                    )
                    failed_items = []
                except Exception as recovery_exc:
                    intent["status"] = "failed"
                    intent["recovery_needed"] = True
                    intent["error"] = f"{first_exc}; retry={retry_exc}; recovery={recovery_exc}"[:500]
                    intent["completed_at"] = _now_iso(self._clock())
                    state["last_purchase_error"] = intent["error"]
                    self._notify_timeout(
                        state,
                        intent,
                        f"intent={intent.get('intent_id')}，首次={first_exc}；重试={retry_exc}；恢复查询={recovery_exc}",
                    )
                    self._save(state)
                    return 0
        except Exception as exc:
            intent["status"] = "failed"
            intent["error"] = f"{type(exc).__name__}: {exc}"[:500]
            state["last_purchase_error"] = intent["error"]
            self._save(state)
            return 0
        intent["recovery_alerted"] = bool(intent.get("recovery_needed") or intent.get("idempotency_retries"))
        return self._finish_intent(state, client, intent, orders, failed_items)

    def _purchase(self, state: dict, quantity: int) -> int:
        if quantity <= 0:
            return 0
        self._clear_recoverable_circuit(state)
        if state.get("circuit", {}).get("open"):
            return 0
        client = self._client()
        api_key_id = self._current_api_key_id(client)
        unresolved = self._unresolved_intents(state)
        if unresolved:
            return self._attempt_intent(state, client, unresolved[0], api_key_id=api_key_id)
        recovered = self._recover_failed_intents(state, client, api_key_id)
        if recovered:
            return recovered
        try:
            self.refresh_balance(state, client)
            # The current API selects Microsoft inventory directly from the
            # project and suffix; no project catalog lookup or product ID is
            # required for purchase orders.
            product = RemailProduct(
                int(_setting("REMAIL_PROJECT_ID")),
                0,
                Decimal("0"),
                str(_setting("REMAIL_EMAIL_SUFFIX")).strip().lower(),
                None,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if any(
                hint in str(exc).lower()
                for hint in ("price", "product", "project", "suffix", "service mode")
            ):
                self._open_circuit(state, reason)
            else:
                state["last_purchase_error"] = reason[:500]
            self._save(state)
            return 0

        allowed = self._allowed_purchase_count(state, quantity)
        if allowed <= 0:
            state["last_purchase_error"] = "Remail per-round purchase limit is zero"
            self._save(state)
            return 0
        logger.info(
            "[Remail] 提交%s下单：requested=%s, quantity=%s",
            "批量" if allowed > 1 else "单笔",
            quantity,
            allowed,
        )
        baseline_order_nos = self._snapshot_order_nos(client, api_key_id)
        intent = {
            "intent_id": str(uuid.uuid4()),
            "status": "pending",
            "quantity": allowed,
            "project_id": product.project_id,
            "product_id": product.product_id,
            "direct_order": True,
            "email_suffix": product.email_suffix,
            "service_mode": str(_setting("REMAIL_SERVICE_MODE")),
            "started_ts": self._clock(),
            "started_at": _now_iso(self._clock()),
            "order_nos": [],
            "error": "",
            "api_key_id": api_key_id,
            "baseline_order_nos": baseline_order_nos,
        }
        state["purchase_intents"].append(intent)
        state["purchase_intents"] = state["purchase_intents"][-1000:]
        self._save(state)
        return self._attempt_intent(state, client, intent, api_key_id=api_key_id)

    def prepare_reservations(self, count: int) -> list[dict]:
        count = max(0, int(count))
        if count == 0 or not configured():
            return []
        with self._lock:
            self.recover()
            state = self._load()
            if any(not order.get("slots") for order in state.get("orders", [])):
                # A paid order may deliver its address after the original POST.
                # Refresh those deliveries before purchasing replacement stock.
                self.sync_known_orders()
                state = self._load()
            def eligible_available_slots(snapshot: dict) -> list[tuple[int, int, dict, dict]]:
                """Return slots safe to submit without blocking on a main job.

                A child address is eligible only after its order's main slot is
                terminal. Fresh orders therefore contribute their main slot
                first; completed orders contribute the child slot first.
                """
                candidates: list[tuple[int, int, dict, dict]] = []
                for order_index, order in enumerate(snapshot.get("orders", [])):
                    slots = order.get("slots", []) or []
                    main_status = next(
                        (
                            str(item.get("status") or "available")
                            for item in slots
                            if str(item.get("kind") or "") == "main"
                        ),
                        "missing",
                    )
                    for slot_index, slot in enumerate(slots):
                        if str(slot.get("status") or "") != "available":
                            continue
                        kind = str(slot.get("kind") or "main")
                        if kind == "child" and main_status not in _TERMINAL_SLOT_STATES:
                            continue
                        candidates.append((order_index, slot_index, order, slot))
                # Completed main -> child addresses are the best next work;
                # otherwise use fresh main addresses. This keeps workers busy
                # without starting a child while its main still owns context.
                candidates.sort(
                    key=lambda item: (
                        0 if str(item[3].get("kind") or "") == "child" else 1,
                        item[0],
                        item[1],
                    )
                )
                return candidates

            candidates = eligible_available_slots(state)
            available = len(candidates)
            if available < count:
                # A fresh order contributes one immediately eligible main;
                # its child remains reserved for a later round.
                needed_orders = count - available
                batch_size = max(1, int(_setting("REMAIL_PURCHASE_BATCH_SIZE")))
                self._purchase(state, max(needed_orders, batch_size))
                state = self._load()
                candidates = eligible_available_slots(state)
            reservations: list[dict] = []
            for _, _, order, slot in candidates:
                if len(reservations) >= count:
                    break
                slot["status"] = "reserved"
                slot["reserved_at"] = _now_iso(self._clock())
                slot["job_id"] = None
                reservations.append({
                    "reservation_id": slot["reservation_id"],
                    "email": slot["email"],
                    "base_email": order["delivery_email"],
                    "address_kind": slot["kind"],
                    "remail_order_no": order["order_no"],
                    "email_source": "remail",
                    "provenance": "remail_auto_purchase",
                })
            self._save(state)
            self._cache_contexts(state)
            return reservations

    def bind_reservation(self, reservation_id: str, job_id: int) -> None:
        with self._lock:
            state = self._load()
            _, slot = self._find_slot(state, reservation_id)
            if not slot or slot.get("status") != "reserved":
                raise RemailError(f"Remail reservation is not available: {reservation_id}")
            slot["job_id"] = int(job_id)
            self._save(state)

    def release_reservation(self, reservation_id: str, *, note: str = "") -> None:
        with self._lock:
            state = self._load()
            _, slot = self._find_slot(state, reservation_id)
            if not slot or slot.get("status") != "reserved":
                return
            slot["status"] = "available"
            slot["job_id"] = None
            slot["reserved_at"] = None
            if note:
                slot["note"] = str(note)[:500]
            self._save(state)

    def release_job(self, job: dict | None, *, note: str = "") -> None:
        reservation_id = str((job or {}).get("mailbox_reservation_id") or "")
        if reservation_id:
            self.release_reservation(reservation_id, note=note)

    def mark_running(self, reservation_id: str, job_id: int) -> None:
        with self._lock:
            state = self._load()
            _, slot = self._find_slot(state, reservation_id)
            if not slot or slot.get("status") != "reserved" or int(slot.get("job_id") or 0) != int(job_id):
                raise RemailError(f"Remail reservation/job binding is invalid: {reservation_id}")
            slot["status"] = "running"
            slot["started_at"] = _now_iso(self._clock())
            self._save(state)

    def finish_reservation(self, reservation_id: str, *, success: bool, note: str = "") -> None:
        with self._lock:
            state = self._load()
            _, slot = self._find_slot(state, reservation_id)
            if not slot or slot.get("status") in _TERMINAL_SLOT_STATES:
                return
            slot["status"] = "succeeded" if success else "failed"
            slot["completed_at"] = _now_iso(self._clock())
            if note:
                slot["note"] = str(note)[:500]
            self._save(state)
            self._cache_contexts(state)

    def invalidate_order_for_email(self, email: str, note: str = "") -> None:
        target = str(email or "").strip().lower()
        with self._lock:
            state = self._load()
            for order in state["orders"]:
                if any(str(slot.get("email") or "").lower() == target for slot in order.get("slots", [])):
                    order["invalid_reason"] = str(note or "invalid pickup token")[:500]
                    for slot in order.get("slots", []):
                        if slot.get("status") not in {"succeeded", "failed"}:
                            slot["status"] = "invalid"
                            slot["completed_at"] = _now_iso(self._clock())
                    self._save(state)
                    self._cache_contexts(state)
                    return

    def context_for_email(self, email: str) -> dict | None:
        target = str(email or "").strip().lower()
        # Pickup reads must remain available while prepare_reservations holds
        # the purchase lock across slow remote HTTP calls.
        with self._context_lock:
            cached = self._context_cache.get(target)
            if cached:
                return dict(cached)
            # The state file is replaced atomically, so reading it without the
            # purchase lock always sees a complete old or new snapshot.
            state = self._load()
            contexts = {}
            for order, slot in self._iter_slots(state):
                context = self._build_context(order, slot)
                if context:
                    contexts[str(slot.get("email") or "").strip().lower()] = context
            self._context_cache.update(contexts)
            found = self._context_cache.get(target)
            return dict(found) if found else None

    def recover_background(self) -> None:
        """Schedule startup recovery once; HTTP status never waits for its I/O."""
        if self._recovered:
            return
        with self._recovery_start_lock:
            if self._recovered or self._recovery_started:
                return
            self._recovery_started = True

        def run():
            try:
                self.recover()
            except Exception as exc:
                logger.error(
                    "event=remail_background_recovery_failed error_type=%s error=%s",
                    type(exc).__name__, safe_error(exc),
                )
            finally:
                with self._recovery_start_lock:
                    self._recovery_started = False

        try:
            threading.Thread(target=run, name="remail-recovery", daemon=True).start()
        except Exception:
            with self._recovery_start_lock:
                self._recovery_started = False
            raise

    @traced("remail_recover")
    def recover(self, *, blocking: bool = True) -> None:
        if self._recovered or not self._lock.acquire(blocking=blocking):
            return
        try:
            if self._recovered:
                return
            state = self._load()
            try:
                from core import db
                jobs = {int(item.get("id") or 0): item for item in db.list_jobs(limit=100000)}
                jobs_by_reservation = {
                    str(item.get("mailbox_reservation_id") or ""): item
                    for item in jobs.values()
                    if str(item.get("mailbox_reservation_id") or "")
                }
                slots_by_reservation = {}
                for _, slot in self._iter_slots(state):
                    # Preserve _find_slot's first-match behavior for old data.
                    slots_by_reservation.setdefault(str(slot.get("reservation_id")), slot)
                    job_id = int(slot.get("job_id") or 0)
                    job = jobs.get(job_id) or jobs_by_reservation.get(
                        str(slot.get("reservation_id") or "")
                    )
                    if job and not job_id:
                        slot["job_id"] = int(job.get("id") or 0) or None
                    status = str((job or {}).get("status") or "")
                    if slot.get("status") == "reserved":
                        if status == "success":
                            slot["status"] = "succeeded"
                            slot["completed_at"] = job.get("completed_at") or _now_iso(self._clock())
                        elif status in {"running", "stopping", "failed", "stopped"}:
                            # No worker survives a process restart. A job that
                            # started may already have consumed the address.
                            slot["status"] = "failed"
                            slot["completed_at"] = job.get("completed_at") or _now_iso(self._clock())
                            slot["note"] = "process restarted after registration started"
                        elif not job or status not in _ACTIVE_JOB_STATES:
                            slot["status"] = "available"
                            slot["job_id"] = None
                            slot["reserved_at"] = None
                    elif slot.get("status") == "running":
                        # A fresh process cannot own a worker from the previous
                        # process; consume the address rather than risk reuse.
                        slot["status"] = "failed"
                        slot["completed_at"] = _now_iso(self._clock())
                        slot["note"] = "process restarted while registration was running"
                        if job and status in {"running", "stopping"}:
                            db.update_job(
                                job_id,
                                status="failed",
                                error="服务重启，Remail 地址按已使用处理",
                                completed_at=_now_iso(self._clock()),
                            )
                # Repair a job written before its reservation binding was saved.
                for job_id, job in jobs.items():
                    reservation_id = str(job.get("mailbox_reservation_id") or "")
                    job_status = str(job.get("status") or "")
                    if (
                        not reservation_id
                        or str(job.get("email_source")) != "remail"
                        or job_status not in _ACTIVE_JOB_STATES
                    ):
                        continue
                    slot = slots_by_reservation.get(reservation_id)
                    if slot and slot.get("status") == "available":
                        slot["status"] = "reserved"
                        slot["job_id"] = job_id
                        slot["reserved_at"] = job.get("created_at") or _now_iso(self._clock())
                logger.info(
                    "event=remail_recovery_bindings jobs=%d indexed_slots=%d",
                    len(jobs), len(slots_by_reservation),
                )

                # Idempotency-Key makes replaying an unfinished POST safe.  A
                # restart or timeout therefore resumes the same intent instead
                # of creating a new order set or permanently opening a circuit.
                unresolved_intents = self._unresolved_intents(state)
                if unresolved_intents and configured():
                    self._clear_recoverable_circuit(state)
                    client = self._client()
                    api_key_id = self._current_api_key_id(client)
                    for intent in unresolved_intents:
                        self._attempt_intent(state, client, intent, api_key_id=api_key_id)
            finally:
                self._save(state)
                self._cache_contexts(state)
                self._recovered = True
        finally:
            self._lock.release()

    def sync_known_orders(self) -> dict:
        if not configured():
            return self.status()
        with self._lock:
            state = self._load()
            client = self._client()
            for order in state["orders"]:
                if order.get("slots") and all(slot.get("status") in _TERMINAL_SLOT_STATES for slot in order["slots"]):
                    continue
                try:
                    detail = client.get_order(order["order_no"])
                    order["remote_status"] = str(detail.get("status") or order.get("remote_status") or "")
                    if detail.get("apiKeyId") is not None:
                        order["api_key_id"] = detail.get("apiKeyId")
                    if detail.get("serviceToken"):
                        order["service_token"] = str(detail["serviceToken"])
                    if detail.get("deliveryEmail"):
                        order["delivery_email"] = str(detail["deliveryEmail"]).strip().lower()
                    email = str(order.get("delivery_email") or "")
                    token = str(order.get("service_token") or "")
                    suffix = str(_setting("REMAIL_EMAIL_SUFFIX")).strip().lower()
                    if not order.get("slots") and token and email.endswith("@" + suffix):
                        order["slots"] = self._make_slots(order["order_no"], email)
                        order["invalid_reason"] = ""
                    order.pop("sync_error", None)
                except Exception as exc:
                    order["sync_error"] = f"{type(exc).__name__}: {exc}"[:500]
            self.refresh_balance(state, client)
            self._save(state)
            return self.status()

    @traced("remail_status")
    def status(self) -> dict:
        # Atomic replacement lets readers use the last committed snapshot
        # while a purchase or recovery is still in progress.
        state = self._load()
        counts = {key: 0 for key in ("available", "reserved", "running", "succeeded", "failed", "invalid")}
        for _, slot in self._iter_slots(state):
            status = str(slot.get("status") or "available")
            counts[status] = counts.get(status, 0) + 1
        total_slots = sum(counts.values())
        hour, day = self._purchase_counts(state)
        balance = decimal_value(state.get("balance_points"))
        threshold = Decimal(str(max(0, int(_setting("REMAIL_LOW_BALANCE_POINTS")))))
        circuit = state.get("circuit") or {}
        is_enabled = bool(_setting("REMAIL_ENABLED")) and not bool(circuit.get("open"))
        is_configured = is_enabled and bool(str(_setting("REMAIL_API_KEY") or "").strip())
        return {
            "instance": _instance_label(),
            "enabled": is_enabled,
            "configured": is_configured,
            "configuration_error": (
                "REMAIL_API_KEY is missing" if is_enabled and not is_configured else ""
            ),
            "project_id": int(_setting("REMAIL_PROJECT_ID")),
            "product_id": self._positive_int(_setting("REMAIL_PRODUCT_ID")),
            "service_mode": str(_setting("REMAIL_SERVICE_MODE")),
            "suffix": str(_setting("REMAIL_EMAIL_SUFFIX")),
            "capacity_per_order": 2,
            "orders": len(state["orders"]),
            "purchased_mailboxes": len(state["orders"]),
            "purchased_email_slots": total_slots,
            "successful_accounts": counts.get("succeeded", 0),
            "available_slots": counts.get("available", 0),
            "reserved_slots": counts.get("reserved", 0),
            "running_slots": counts.get("running", 0),
            "succeeded_slots": counts.get("succeeded", 0),
            "failed_slots": counts.get("failed", 0),
            "invalid_slots": counts.get("invalid", 0),
            "invalid_orders": sum(1 for item in state["orders"] if item.get("invalid_reason")),
            "purchase_circuit_open": bool(circuit.get("open")),
            "purchase_circuit_reason": str(circuit.get("reason") or ""),
            "purchased_this_hour": hour,
            "purchased_today": day,
            "balance_points": str(balance) if balance is not None else None,
            "low_balance": bool(balance is not None and balance <= threshold),
            "last_balance_at": state.get("last_balance_at"),
            "last_purchase_at": state.get("last_purchase_at"),
            "last_purchase_error": str(state.get("last_purchase_error") or ""),
            "recovering_purchase_intents": len(self._unresolved_intents(state)) + sum(
                1 for item in state.get("purchase_intents", [])
                if self._is_historical_recovery_intent(item)
            ),
        }


remail_manager = RemailManager()
