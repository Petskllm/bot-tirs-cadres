from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import requests

from bot_tirs_cadres.config import AppConfig
from bot_tirs_cadres.storage.database import SQLiteStore

logger = logging.getLogger(__name__)


class BzzoiroError(RuntimeError):
    """Erreur lisible retournée par l'API ou le transport HTTP."""


class DailyQuotaExceeded(BzzoiroError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class APIResponseError(BzzoiroError):
    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"Bzzoiro HTTP {status}: {message}")
        self.status = status
        self.payload = payload


class BzzoiroHTTPClient:
    def __init__(
        self,
        config: AppConfig,
        store: SQLiteStore,
        *,
        session: requests.Session | None = None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        self.config = config
        self.store = store
        self.session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic
        self._request_times: deque[float] = deque()

    def _cache_key(self, url: str, params: dict[str, Any] | None) -> str:
        normalized = urlencode(sorted((str(k), str(v)) for k, v in (params or {}).items()), doseq=True)
        return hashlib.sha256(f"{url}?{normalized}".encode()).hexdigest()

    def _throttle(self) -> None:
        cap = max(1, self.config.burst_requests_per_second)
        now = self._monotonic()
        while self._request_times and now - self._request_times[0] >= 1:
            self._request_times.popleft()
        if len(self._request_times) >= cap:
            delay = max(0.0, 1.0 - (now - self._request_times[0]))
            self._sleep(delay)
            now = self._monotonic()
            while self._request_times and now - self._request_times[0] >= 1:
                self._request_times.popleft()
        self._request_times.append(now)

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        auth_required: bool = True,
        ttl_seconds: int | None = None,
    ) -> Any:
        url = self.config.api_base_url.rstrip("/") + "/" + path.lstrip("/")
        cache_key = self._cache_key(url, params)
        cached = self.store.cache_get(cache_key)
        if cached is not None:
            logger.debug("Cache hit: %s", path)
            return cached

        headers = {"Accept": "application/json"}
        if auth_required:
            if not self.config.api_token:
                raise BzzoiroError(f"Jeton API absent ({self.config.api_token_env}).")
            headers["Authorization"] = f"Token {self.config.api_token}"

        for attempt in range(self.config.max_retries + 1):
            used = self.store.request_count_today()
            if used >= self.config.daily_request_limit:
                raise DailyQuotaExceeded(
                    f"Quota local atteint ({used}/{self.config.daily_request_limit} requêtes UTC)."
                )
            self._throttle()
            self.store.increment_request_count()
            try:
                response = self.session.get(
                    url, params=params or {}, headers=headers, timeout=self.config.timeout_seconds
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.config.max_retries:
                    raise BzzoiroError(f"Erreur réseau Bzzoiro après {attempt + 1} tentative(s): {exc}") from exc
                delay = min(2**attempt, self.config.max_backoff_seconds)
                logger.warning("Erreur réseau (%s), nouvel essai dans %.1fs", exc, delay)
                self._sleep(delay)
                continue

            if response.status_code == 429:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                code = payload.get("code") if isinstance(payload, dict) else None
                retry_after = self._retry_after(response)
                if code == "taster_exhausted":
                    raise DailyQuotaExceeded(
                        "Quota quotidien Bzzoiro épuisé; le serveur indique le délai avant réinitialisation.",
                        retry_after,
                    )
                if attempt >= self.config.max_retries:
                    raise APIResponseError(429, "limite de débit atteinte après plusieurs tentatives", payload)
                wait = retry_after if retry_after is not None else min(2**attempt, self.config.max_backoff_seconds)
                # Ne pas dormir des heures sur un quota journalier non identifié.
                if wait > self.config.max_backoff_seconds:
                    raise DailyQuotaExceeded("Réessai différé par Retry-After; arrêt pour ne pas bloquer le bot.", wait)
                logger.warning("HTTP 429 (%s), nouvel essai dans %.1fs", code or "sans code", wait)
                self._sleep(wait)
                continue

            if response.status_code >= 500 and attempt < self.config.max_retries:
                delay = min(2**attempt, self.config.max_backoff_seconds)
                logger.warning("Bzzoiro HTTP %s, nouvel essai dans %.1fs", response.status_code, delay)
                self._sleep(delay)
                continue

            if not response.ok:
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                message = payload.get("detail", response.reason) if isinstance(payload, dict) else response.reason
                raise APIResponseError(response.status_code, str(message), payload)

            try:
                data = response.json()
            except ValueError as exc:
                raise BzzoiroError(f"Réponse JSON invalide pour {path}.") from exc
            ttl = self.config.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
            if ttl > 0:
                self.store.cache_set(cache_key, data, ttl)
            return data

        raise BzzoiroError("Échec inattendu du client Bzzoiro.")
