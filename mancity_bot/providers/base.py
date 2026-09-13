"""Shared plumbing for data providers: throttling, retries, error type."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A provider could not answer. Callers should degrade, not crash."""


class PlanLimitError(ProviderError):
    """The account's subscription tier does not cover this request.

    Distinct from a transient failure: retrying cannot help, so callers should
    stop asking rather than spend more of a metered quota.
    """


class Provider:
    """Base HTTP client with a token-bucket throttle and 429-aware retries.

    Both free tiers we target are tightly rate limited (football-data.org allows
    10 requests/minute), so every call goes through the same gate.
    """

    name = "provider"
    base_url = ""
    #: minimum seconds between two outbound requests
    min_interval = 0.0

    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "mancity-bot/1.0"},
        )
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {}

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, attempts: int = 3
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            async with self._lock:
                wait = self.min_interval - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()
            try:
                resp = await self._client.get(
                    path, params=params, headers=self._headers()
                )
            except httpx.HTTPError as exc:  # network / timeout
                last_error = exc
                log.warning("%s %s failed (%s), attempt %d", self.name, path, exc, attempt)
                await asyncio.sleep(2 * attempt)
                continue

            if resp.status_code == 429:
                delay = float(resp.headers.get("Retry-After", 10 * attempt))
                log.warning("%s rate limited, sleeping %.0fs", self.name, delay)
                await asyncio.sleep(min(delay, 60))
                last_error = ProviderError(f"{self.name}: rate limited")
                continue
            if resp.status_code in (401, 403):
                raise ProviderError(
                    f"{self.name}: credentials rejected (HTTP {resp.status_code}). "
                    "Check the API key in .env."
                )
            if resp.status_code >= 500:
                last_error = ProviderError(f"{self.name}: HTTP {resp.status_code}")
                await asyncio.sleep(2 * attempt)
                continue
            if resp.status_code >= 400:
                raise ProviderError(f"{self.name}: HTTP {resp.status_code} for {path}")

            try:
                return resp.json()
            except ValueError as exc:
                raise ProviderError(f"{self.name}: malformed JSON from {path}") from exc

        raise ProviderError(f"{self.name}: {path} failed after {attempts} attempts") from last_error
