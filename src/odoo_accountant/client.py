"""Odoo 19 JSON-2 client. Public methods are read-only; mutation is internal."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import Config
from .errors import OdooClientError, OdooError
from .models import ExecutionAuthorization, Risk
from .policy import READ_ODOO_METHODS, classify_odoo_call

Transport = Callable[[str, dict, bytes, float], "tuple[int, bytes]"]


def _make_urllib_transport(ca_bundle: str | None) -> Transport:
    ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()

    def transport(url: str, headers: dict, body: bytes, timeout: float):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OdooClientError(f"Connection error: {type(exc).__name__}") from None

    return transport


class OdooClient:
    def __init__(self, config: Config, transport: Transport | None = None, read_only: bool = False):
        config.require_odoo()
        self.config = config
        self.read_only = read_only
        self._transport = transport or _make_urllib_transport(config.ca_bundle)

    # ---- low level -------------------------------------------------
    def _post(self, model: str, method: str, body: dict) -> Any:
        url = f"{self.config.url}/json/2/{model}/{method}"
        headers = {"Content-Type": "application/json", "X-Odoo-Database": self.config.db}
        if self.config.token:
            headers["Authorization"] = "Bearer " + self.config.token
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, payload = self._transport(url, headers, raw, self.config.timeout)
        try:
            data = json.loads(payload.decode("utf-8")) if payload else None
        except ValueError:
            raise OdooClientError(f"Non-JSON response (HTTP {status})") from None
        if status >= 400:
            info = data if isinstance(data, dict) else {}
            raise OdooError(status, str(info.get("name", "error")), str(info.get("message", ""))[:300])
        return data

    def _call(self, model: str, method: str, body: dict) -> Any:
        if method not in READ_ODOO_METHODS:
            raise OdooClientError(f"Method '{method}' is not a read method; blocked")
        try:
            return self._post(model, method, body)
        except OdooClientError as exc:
            if isinstance(exc, OdooError) and exc.status < 500:
                raise
            return self._post(model, method, body)  # one retry for transient failures

    def _mutate(self, model: str, method: str, body: dict, authorization: ExecutionAuthorization) -> Any:
        """Internal: only the executor calls this, with a consumed-approval authorization."""
        if not isinstance(authorization, ExecutionAuthorization):
            raise PermissionError("Mutation requires an ExecutionAuthorization")
        if self.read_only:
            raise OdooClientError("Client is read-only")
        risk = classify_odoo_call(model, method)
        if risk in (Risk.READ, Risk.DESTRUCTIVE):
            raise OdooClientError(f"Method '{model}.{method}' is not permitted as a mutation")
        return self._post(model, method, body)

    # ---- read API ---------------------------------------------------
    def search_count(self, model: str, domain: list) -> int:
        return int(self._call(model, "search_count", {"domain": domain}))

    def search_read(self, model, domain, fields, limit=None, order=None, offset=None) -> list:
        body: dict = {"domain": domain, "fields": fields}
        if limit is not None:
            body["limit"] = limit
        if order:
            body["order"] = order
        if offset:
            body["offset"] = offset
        return self._call(model, "search_read", body)

    def read(self, model: str, ids: list, fields: list) -> list:
        return self._call(model, "read", {"ids": ids, "fields": fields})

    def formatted_read_group(self, model, domain, groupby, aggregates, order=None, limit=None) -> list:
        body: dict = {"domain": domain, "groupby": groupby, "aggregates": aggregates}
        if order:
            body["order"] = order
        if limit is not None:
            body["limit"] = limit
        return self._call(model, "formatted_read_group", body)

    def fields_get(self, model: str, attributes: list | None = None) -> dict:
        return self._call(model, "fields_get", {"attributes": attributes or ["string", "type"]})
