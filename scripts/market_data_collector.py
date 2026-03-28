from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

BOOTSTRAP_ROOT = Path(__file__).resolve().parent.parent
if str(BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(BOOTSTRAP_ROOT))

from core.exchange import PolymarketExchange, _to_float
from core.market_resolver import resolve_latest_btc_5m_token_ids
from core.runtime_paths import ROOT_DIR, trade_journal_path
from core.ws_binance import BINANCE_WS


MARKET_DATA_DIR = ROOT_DIR / "market_data"
ISO_TS_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _now_iso_ms() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def parse_event_ts(ts: str | None) -> float:
    raw = str(ts or "").strip()
    if not raw:
        return time.time()
    try:
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return time.time()


def safe_fragment(text: str | None, *, max_len: int = 96) -> str:
    raw = ISO_TS_SAFE_RE.sub("_", str(text or "").strip()).strip("._-")
    if not raw:
        return "unknown"
    return raw[:max_len]


def slim_orderbook(book: dict | None, *, depth: int = 5) -> dict[str, Any]:
    if not isinstance(book, dict):
        return {}
    return {
        "best_bid": _to_float(book.get("best_bid"), 0.0),
        "best_ask": _to_float(book.get("best_ask"), 0.0),
        "best_bid_size": _to_float(book.get("best_bid_size"), 0.0),
        "best_ask_size": _to_float(book.get("best_ask_size"), 0.0),
        "bids_volume": _to_float(book.get("bids_volume"), 0.0),
        "asks_volume": _to_float(book.get("asks_volume"), 0.0),
        "bid_levels": [
            {"price": _to_float(price, 0.0), "size": _to_float(size, 0.0)}
            for price, size in list(book.get("bid_levels") or [])[:depth]
        ],
        "ask_levels": [
            {"price": _to_float(price, 0.0), "size": _to_float(size, 0.0)}
            for price, size in list(book.get("ask_levels") or [])[:depth]
        ],
    }


def summarize_binance_trades(trades: list[dict[str, Any]]) -> dict[str, float]:
    buy_qty = 0.0
    sell_qty = 0.0
    buy_count = 0
    sell_count = 0
    notional = 0.0
    for trade in trades:
        price = _to_float(trade.get("p"), 0.0)
        qty = _to_float(trade.get("q"), 0.0)
        maker_is_seller = bool(trade.get("m", False))
        notional += price * qty
        if maker_is_seller:
            buy_qty += qty
            buy_count += 1
        else:
            sell_qty += qty
            sell_count += 1
    return {
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "buy_count": float(buy_count),
        "sell_count": float(sell_count),
        "net_qty": buy_qty - sell_qty,
        "gross_notional": notional,
    }


def build_capture_dir_name(event: dict[str, Any]) -> str:
    ts_part = safe_fragment(str(event.get("ts") or _now_iso_ms()), max_len=40)
    kind = safe_fragment(str(event.get("kind") or "event"), max_len=16)
    side = safe_fragment(str(event.get("side") or "NA"), max_len=12)
    slug = safe_fragment(str(event.get("slug") or "unknown-market"), max_len=72)
    event_id = safe_fragment(str(event.get("event_id") or "evt"), max_len=24)
    return f"{ts_part}_{kind}_{side}_{slug}_{event_id}"


def fetch_market_by_slug(slug: str) -> dict[str, Any] | None:
    try:
        from core.market_resolver import _fetch_by_slug  # Local import keeps script isolated.

        return _fetch_by_slug(slug)
    except Exception:
        return None


def collect_snapshot(
    ex: PolymarketExchange,
    *,
    slug: str,
    market_meta: dict[str, Any],
    depth: int,
) -> dict[str, Any]:
    ts_unix = time.time()
    ts_iso = _now_iso_ms()
    token_up = str(market_meta.get("token_up") or "")
    token_down = str(market_meta.get("token_down") or "")
    up_book = slim_orderbook(ex.get_full_orderbook(token_up), depth=depth) if token_up else {}
    down_book = slim_orderbook(ex.get_full_orderbook(token_down), depth=depth) if token_down else {}

    bba = BINANCE_WS.get_bba()
    bid = _to_float(bba.get("b"), 0.0)
    ask = _to_float(bba.get("a"), 0.0)
    mid = (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else 0.0
    trades_3s = BINANCE_WS.get_recent_trades(seconds=3.0)
    trades_10s = BINANCE_WS.get_recent_trades(seconds=10.0)

    return {
        "ts": ts_iso,
        "ts_unix": ts_unix,
        "slug": slug,
        "question": market_meta.get("question"),
        "token_up": token_up,
        "token_down": token_down,
        "polymarket": {
            "up": up_book,
            "down": down_book,
        },
        "binance": {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "bid_size": _to_float(bba.get("B"), 0.0),
            "ask_size": _to_float(bba.get("A"), 0.0),
            "update_age_sec": float(BINANCE_WS.get_last_update_age()),
            "velocity_3s": float(BINANCE_WS.get_price_velocity(3.0)),
            "velocity_10s": float(BINANCE_WS.get_price_velocity(10.0)),
            "trades_3s": summarize_binance_trades(trades_3s),
            "trades_10s": summarize_binance_trades(trades_10s),
        },
    }


@dataclass
class CaptureTask:
    event: dict[str, Any]
    started_at: float
    ends_at: float
    out_dir: Path
    event_path: Path
    window_path: Path
    written_ts: set[float] = field(default_factory=set)
    finalized: bool = False

    @property
    def slug(self) -> str:
        return str(self.event.get("slug") or "")

    @property
    def event_ts(self) -> float:
        return parse_event_ts(self.event.get("ts"))

    def write_metadata(self, *, market_meta: dict[str, Any] | None, pre_seconds: float, post_seconds: float, poll_sec: float) -> None:
        payload = {
            "collector_started_at": _now_iso_ms(),
            "pre_seconds": pre_seconds,
            "post_seconds": post_seconds,
            "poll_sec": poll_sec,
            "event": self.event,
            "market": market_meta or {},
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.event_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_snapshot(self, snapshot: dict[str, Any]) -> bool:
        ts_unix = float(snapshot.get("ts_unix") or 0.0)
        if ts_unix in self.written_ts:
            return False
        if ts_unix < self.started_at - 0.5 or ts_unix > self.ends_at + 0.5:
            return False
        row = {
            **snapshot,
            "event_id": self.event.get("event_id"),
            "event_kind": self.event.get("kind"),
            "event_side": self.event.get("side"),
            "event_reason": self.event.get("reason") or self.event.get("entry_reason"),
            "relative_sec": round(ts_unix - self.event_ts, 3),
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with self.window_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.written_ts.add(ts_unix)
        return True


class JournalTail:
    def __init__(self, path: Path, *, start_at_end: bool = True):
        self.path = path
        self.offset = path.stat().st_size if (start_at_end and path.exists()) else 0

    def poll(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0
            with self.path.open("r", encoding="utf-8") as fh:
                fh.seek(self.offset)
                lines = fh.readlines()
                self.offset = fh.tell()
        except Exception:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if str(row.get("kind") or "") not in {"entry", "exit"}:
                continue
            if not row.get("slug"):
                continue
            events.append(row)
        return events


def create_capture_task(
    event: dict[str, Any],
    *,
    pre_seconds: float,
    post_seconds: float,
) -> CaptureTask:
    event_ts = parse_event_ts(event.get("ts"))
    day_dir = MARKET_DATA_DIR / datetime.fromtimestamp(event_ts).strftime("%Y-%m-%d")
    out_dir = day_dir / build_capture_dir_name(event)
    return CaptureTask(
        event=event,
        started_at=event_ts - pre_seconds,
        ends_at=event_ts + post_seconds,
        out_dir=out_dir,
        event_path=out_dir / "event.json",
        window_path=out_dir / "window.jsonl",
    )


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Collect Polymarket/Binance snapshots 30s before and after entry/exit journal events.")
    ap.add_argument("--mode", choices=["live", "dryrun"], default="live", help="Which trade journal to watch.")
    ap.add_argument("--pre-seconds", type=float, default=30.0, help="Seconds of pre-event snapshots to keep.")
    ap.add_argument("--post-seconds", type=float, default=30.0, help="Seconds of post-event snapshots to keep.")
    ap.add_argument("--poll-sec", type=float, default=1.0, help="Polling interval for market snapshots.")
    ap.add_argument("--book-depth", type=int, default=5, help="How many bid/ask levels to persist.")
    ap.add_argument("--start-from-beginning", action="store_true", help="Read the whole journal instead of tailing from the end.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    MARKET_DATA_DIR.mkdir(parents=True, exist_ok=True)

    journal_path = trade_journal_path(dry_run=(args.mode == "dryrun"))
    tail = JournalTail(journal_path, start_at_end=not args.start_from_beginning)
    ex = PolymarketExchange(dry_run=True)
    BINANCE_WS.start()

    running = True

    def _stop(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    recent_buffers: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=max(120, int((args.pre_seconds + 10.0) / max(args.poll_sec, 0.2)) + 5)))
    active_captures: list[CaptureTask] = []
    seen_event_ids: set[str] = set()
    market_cache: dict[str, dict[str, Any]] = {}
    watch_slugs: dict[str, float] = {}
    last_latest_refresh = 0.0

    print(f"[collector] watching journal: {journal_path}")
    print(f"[collector] writing market windows to: {MARKET_DATA_DIR}")

    while running:
        loop_ts = time.time()

        for ev in tail.poll():
            event_id = str(ev.get("event_id") or "")
            if event_id and event_id in seen_event_ids:
                continue
            if event_id:
                seen_event_ids.add(event_id)
            slug = str(ev.get("slug") or "")
            if not slug:
                continue
            market_meta = market_cache.get(slug) or fetch_market_by_slug(slug)
            if market_meta:
                market_cache[slug] = market_meta
            task = create_capture_task(ev, pre_seconds=args.pre_seconds, post_seconds=args.post_seconds)
            task.write_metadata(
                market_meta=market_meta,
                pre_seconds=args.pre_seconds,
                post_seconds=args.post_seconds,
                poll_sec=args.poll_sec,
            )
            for snapshot in list(recent_buffers.get(slug, [])):
                task.append_snapshot(snapshot)
            active_captures.append(task)
            watch_slugs[slug] = max(watch_slugs.get(slug, 0.0), task.ends_at + 5.0)
            print(f"[collector] armed capture | kind={ev.get('kind')} side={ev.get('side')} slug={slug} out={task.out_dir}")

        if loop_ts - last_latest_refresh >= max(2.0, args.poll_sec):
            try:
                latest_market = resolve_latest_btc_5m_token_ids()
            except Exception:
                latest_market = None
            if latest_market and latest_market.get("slug"):
                slug = str(latest_market["slug"])
                market_cache[slug] = latest_market
                watch_slugs[slug] = max(watch_slugs.get(slug, 0.0), loop_ts + args.pre_seconds + 10.0)
            last_latest_refresh = loop_ts

        watched_now = sorted(slug for slug, expiry in watch_slugs.items() if expiry >= loop_ts)
        for slug in watched_now:
            market_meta = market_cache.get(slug) or fetch_market_by_slug(slug)
            if not market_meta:
                continue
            market_cache[slug] = market_meta
            snapshot = collect_snapshot(ex, slug=slug, market_meta=market_meta, depth=args.book_depth)
            buf = recent_buffers[slug]
            buf.append(snapshot)
            while buf and (loop_ts - float(buf[0].get("ts_unix") or 0.0)) > (args.pre_seconds + 10.0):
                buf.popleft()
            for task in active_captures:
                if not task.finalized and task.slug == slug:
                    task.append_snapshot(snapshot)

        still_active: list[CaptureTask] = []
        for task in active_captures:
            if loop_ts <= task.ends_at + 0.5:
                still_active.append(task)
                continue
            if not task.finalized:
                task.finalized = True
                print(f"[collector] capture complete | event_id={task.event.get('event_id')} rows={len(task.written_ts)} out={task.out_dir}")
        active_captures = still_active

        time.sleep(max(0.2, float(args.poll_sec)))

    BINANCE_WS.stop()
    print("[collector] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
