"""Small, strict client for the Remail Open API."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from core.diagnostics import diagnostic_span

logger = logging.getLogger(__name__)


class RemailError(RuntimeError):
    """Remail rejected a request or returned an unusable response."""


class RemailAmbiguousPurchaseError(RemailError):
    """A POST result is unknown; retry only with the same Idempotency-Key."""


@dataclass(frozen=True)
class RemailProduct:
    project_id: int
    product_id: int
    purchase_price: Decimal
    email_suffix: str
    public_available: int | None


def _safe_error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "").strip()
            request_id = str(payload.get("requestId") or "").strip()
            detail = message or f"HTTP {response.status_code}"
            return f"{detail} (requestId={request_id})" if request_id else detail
    except Exception:
        pass
    return f"HTTP {response.status_code}: {(response.text or '')[:240]}"


class RemailClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout = max(1.0, float(timeout))
        if not self.base_url:
            raise RemailError("REMAIL_BASE_URL is empty")
        if not self.api_key:
            raise RemailError("REMAIL_API_KEY is empty")
        if not self.api_key.startswith("rk-"):
            raise RemailError("REMAIL_API_KEY must start with rk-")
        self.session = requests.Session()

    def _request(self, method: str, path: str, *, ambiguous_post: bool = False, **kwargs) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "gpt-register-remail/1.0",
        })
        try:
            with diagnostic_span("remail_http", method=method, path=path):
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=self.timeout,
                    **kwargs,
                )
                logger.info(
                    "event=remail_http_response method=%s path=%s status_code=%s",
                    method, path, response.status_code,
                )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if ambiguous_post and method.upper() == "POST":
                raise RemailAmbiguousPurchaseError(
                    f"Remail purchase result is unknown: {type(exc).__name__}"
                ) from exc
            raise RemailError(f"Remail request failed: {type(exc).__name__}: {exc}") from exc
        except requests.RequestException as exc:
            raise RemailError(f"Remail request failed: {type(exc).__name__}: {exc}") from exc

        if not 200 <= response.status_code < 300:
            detail = _safe_error_text(response)
            if (
                ambiguous_post
                and method.upper() == "POST"
                and response.status_code in {409, 425, 429, 500, 502, 503, 504}
            ):
                raise RemailAmbiguousPurchaseError(
                    f"Remail purchase remains retryable: {detail}"
                )
            raise RemailError(detail)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            if ambiguous_post and method.upper() == "POST":
                raise RemailAmbiguousPurchaseError("Remail purchase returned non-JSON content") from exc
            raise RemailError("Remail returned non-JSON content") from exc

    def get_project(self, project_id: int) -> dict:
        payload = self._request("GET", f"/v1/open/projects/{int(project_id)}")
        if not isinstance(payload, dict):
            raise RemailError("Remail project response is not an object")
        return payload

    def get_api_key_profile(self) -> dict:
        """Return the authenticated API Key profile (including its numeric ID)."""
        payload = self._request("GET", "/v1/open/apikey/profile")
        if isinstance(payload, dict) and isinstance(payload.get("apiKey"), dict):
            payload = payload["apiKey"]
        if not isinstance(payload, dict):
            raise RemailError("Remail API Key profile response is not an object")
        return payload

    def get_wallet(self) -> dict:
        payload = self._request("GET", "/v1/open/wallet")
        if not isinstance(payload, dict):
            raise RemailError("Remail wallet response is not an object")
        return payload

    def list_orders(self, *, limit: int = 100) -> list[dict]:
        payload = self._request("GET", "/v1/open/orders", params={"limit": max(1, min(100, limit))})
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            items = payload.get("items") or payload.get("orders") or []
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        raise RemailError("Remail order list has an unsupported shape")

    def get_order(self, order_no: str) -> dict:
        payload = self._request("GET", f"/v1/open/orders/{order_no}")
        if isinstance(payload, dict) and isinstance(payload.get("order"), dict):
            payload = payload["order"]
        if not isinstance(payload, dict):
            raise RemailError("Remail order detail is not an object")
        return payload

    def create_orders(
        self,
        *,
        project_id: int,
        product_id: int | None,
        email_suffix: str,
        service_mode: str,
        quantity: int,
        idempotency_key: str | None = None,
        supply: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        quantity = int(quantity)
        if quantity < 1 or quantity > 100:
            raise ValueError("quantity must be between 1 and 100")
        request_key = str(idempotency_key or "").strip() or str(uuid.uuid4())
        headers = {"Idempotency-Key": request_key}
        body = {
            "projectId": int(project_id),
            "emailSuffix": str(email_suffix),
        }
        # The current Remail API selects the product by project and suffix.
        # Keep accepting product_id for compatibility with older callers, but
        # omit it when unset so the server can perform the selection.
        if product_id is not None and int(product_id) > 0:
            body["productId"] = int(product_id)
        params = {"serviceMode": str(service_mode)}
        if supply:
            params["supply"] = str(supply)
        if quantity == 1:
            payload = self._request(
                "POST",
                "/v1/open/orders",
                params=params,
                json=body,
                headers=headers,
                ambiguous_post=True,
            )
            if isinstance(payload, dict) and isinstance(payload.get("order"), dict):
                payload = payload["order"]
            if not isinstance(payload, dict):
                raise RemailError("Remail create-order response is not an object")
            return [payload], []

        # The current API's suffix-selected purchase path does not use the
        # batch endpoint. Submit deterministic single orders so retries with
        # the same parent idempotency key converge per mailbox.
        if supply == "public_only":
            succeeded: list[dict] = []
            failed: list[dict] = []
            for index in range(quantity):
                child_key = f"{request_key}:{index + 1}"[:128]
                try:
                    payload = self._request(
                        "POST",
                        "/v1/open/orders",
                        params=params,
                        json=body,
                        headers={"Idempotency-Key": child_key},
                        ambiguous_post=True,
                    )
                    if isinstance(payload, dict) and isinstance(payload.get("order"), dict):
                        payload = payload["order"]
                    if isinstance(payload, dict):
                        succeeded.append(payload)
                    else:
                        failed.append({"index": index, "status": "failed", "error": "invalid order response"})
                except RemailAmbiguousPurchaseError:
                    raise
                except RemailError as exc:
                    failed.append({"index": index, "status": "failed", "error": {"message": str(exc)}})
            return succeeded, failed

        batch_body = dict(body)
        batch_body["quantity"] = quantity
        payload = self._request(
            "POST",
            "/v1/open/orders/batch",
            params=params,
            json=batch_body,
            headers=headers,
            ambiguous_post=True,
        )
        items = payload
        if isinstance(payload, dict):
            items = payload.get("items") or payload.get("results") or payload.get("orders")
        if not isinstance(items, list):
            raise RemailError("Remail batch-order response is not a list")
        succeeded: list[dict] = []
        failed: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            order = item.get("order")
            if item.get("status") == "succeeded" and isinstance(order, dict):
                succeeded.append(order)
            elif isinstance(order, dict) and item.get("status") not in {"failed", "error"}:
                succeeded.append(order)
            else:
                failed.append(item)
        return succeeded, failed


def decimal_value(value: Any) -> Decimal | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
