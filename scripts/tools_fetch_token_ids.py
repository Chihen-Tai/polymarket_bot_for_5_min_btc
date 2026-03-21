#!/usr/bin/env python3
import argparse
import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="market slug, e.g. btc-updown-5m-1773691500")
    args = ap.parse_args()

    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"slug": args.slug},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()

    if not data:
        raise SystemExit("No market found for this slug")

    m = data[0]
    ids = m.get("clobTokenIds") or []
    if isinstance(ids, str):
        import json
        ids = json.loads(ids)
    print("question:", m.get("question"))
    print("slug:", m.get("slug"))
    print("conditionId:", m.get("conditionId"))
    print("clobTokenIds:", ids)

    if len(ids) >= 2:
        print("TOKEN_ID_UP=", ids[0])
        print("TOKEN_ID_DOWN=", ids[1])


if __name__ == "__main__":
    main()
