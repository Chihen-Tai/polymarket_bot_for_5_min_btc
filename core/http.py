from __future__ import annotations

import time
from typing import Any

import requests


DEFAULT_TIMEOUT = 12
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    backoff_base: float = 1.0,
):
    """GET JSON with exponential backoff on rate-limit (429) and transient server errors."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=timeout, verify=True)
            if response.status_code in _RETRY_STATUS_CODES and attempt < retries - 1:
                time.sleep(backoff_base * (2 ** attempt))
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff_base * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


def request_json_with_session(
    session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    backoff_base: float = 1.0,
):
    """Session-based GET JSON with exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=timeout, verify=True)
            if response.status_code in _RETRY_STATUS_CODES and attempt < retries - 1:
                time.sleep(backoff_base * (2 ** attempt))
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff_base * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
