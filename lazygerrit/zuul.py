"""Read-only client for the Zuul REST API (anonymous access)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


class ZuulError(Exception):
    pass


class ZuulClient:
    def __init__(self, base_url: str, tenant: str, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant
        self.timeout = timeout

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/api/tenant/{self.tenant}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise ZuulError(f"Zuul HTTP {e.code} for {path}") from e
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise ZuulError(f"Zuul request failed: {e}") from e

    def change_status(self, number: int, patch_set: int) -> list[dict]:
        data = self._get(f"/status/change/{number},{patch_set}")
        return data if isinstance(data, list) else []

    def buildsets(self, number: int, patch_set: Optional[int] = None, limit: int = 10) -> list[dict]:
        params: dict[str, Any] = {"change": number, "limit": limit}
        if patch_set is not None:
            params["patchset"] = patch_set
        data = self._get("/buildsets", params)
        return data if isinstance(data, list) else []

    def status_page(self, number: int, patch_set: Optional[int] = None) -> str:
        if patch_set:
            return f"{self.base_url}/t/{self.tenant}/status/change/{number},{patch_set}"
        return f"{self.base_url}/t/{self.tenant}/status?change={number}"

    def build_page(self, uuid: str) -> str:
        return f"{self.base_url}/t/{self.tenant}/build/{uuid}"
