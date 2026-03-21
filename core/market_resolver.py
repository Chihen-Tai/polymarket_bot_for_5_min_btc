from datetime import datetime, timezone
import json
import time

import requests

from core.config import SETTINGS


class MarketResolutionError(Exception):
    pass


def _coerce_ids(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    return v if isinstance(v, list) else []


def _fetch_by_slug(slug: str):
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"slug": slug},
        timeout=12,
    )
    r.raise_for_status()
    arr = r.json() or []
    if not arr:
        return None
    m = arr[0]
    ids = _coerce_ids(m.get("clobTokenIds"))
    if len(ids) < 2:
        return None
    return {
        "question": m.get("question"),
        "slug": m.get("slug") or slug,
        "condition_id": m.get("conditionId"),
        "token_up": str(ids[0]),
        "token_down": str(ids[1]),
        "outcomes": m.get("outcomes"),
        "outcomePrices": m.get("outcomePrices"),
        "endDate": m.get("endDate") or m.get("end_date_iso"),
    }


def _candidate_slugs_from_epoch(prefix: str):
    # 使用者觀察：每 5 分鐘 +300
    now = int(time.time())
    base = (now // 300) * 300
    # 依序嘗試當前區間與前後幾檔
    for d in [0, 300, -300, 600, -600, 900, -900]:
        yield f"{prefix}{base + d}"


def resolve_latest_btc_5m_token_ids() -> dict:
    prefix = SETTINGS.market_slug_prefix

    # 先走「5 分鐘 +300」規律（最穩）
    for slug in _candidate_slugs_from_epoch(prefix):
        got = _fetch_by_slug(slug)
        if got:
            return got

    # 後備：掃 active markets 用 prefix contains
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"active": "true", "closed": "false", "limit": 500},
        timeout=12,
    )
    r.raise_for_status()
    data = r.json()

    candidates = []
    for m in data:
        slug = (m.get("slug") or "")
        if prefix.lower() not in slug.lower():
            continue
        ids = _coerce_ids(m.get("clobTokenIds"))
        if len(ids) < 2:
            continue
        # 優先取 slug 最後的 epoch 數字
        try:
            tail = int(slug.split("-")[-1])
        except Exception:
            tail = 0
        candidates.append((tail, m, ids))

    if not candidates:
        raise MarketResolutionError(f"no active markets with slug prefix: {prefix}")

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, m, ids = candidates[0]
    return {
        "question": m.get("question"),
        "slug": m.get("slug"),
        "condition_id": m.get("conditionId"),
        "token_up": str(ids[0]),
        "token_down": str(ids[1]),
        "outcomes": m.get("outcomes"),
        "outcomePrices": m.get("outcomePrices"),
        "endDate": m.get("endDate") or m.get("end_date_iso"),
    }
