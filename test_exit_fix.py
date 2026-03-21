from exchange import PolymarketExchange


def main():
    ex = PolymarketExchange(dry_run=True)

    value, source, fields = ex._extract_close_response_value({"takingAmount": 0.9823, "makingAmount": 2.094658})
    filled, filled_source = ex._extract_close_response_filled_shares({"takingAmount": 0.9823, "makingAmount": 2.094658})

    cases = [
        ("close_response_value_prefers_taking_amount", abs((value or 0.0) - 0.9823) < 1e-9),
        ("close_response_value_source", source == "close_response_takingAmount"),
        ("close_response_filled_shares_from_making_amount", abs((filled or 0.0) - 2.094658) < 1e-9),
        ("close_response_filled_shares_source", filled_source == "close_response_makingAmount"),
        ("close_response_fields_keep_both", fields.get("takingAmount") == 0.9823 and fields.get("makingAmount") == 2.094658),
    ]

    failed = [name for name, ok in cases if not ok]
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("OK")


if __name__ == "__main__":
    main()
