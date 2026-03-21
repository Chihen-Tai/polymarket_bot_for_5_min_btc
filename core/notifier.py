import json
import requests


def notify_discord(webhook_url: str, text: str):
    if not webhook_url:
        return
    try:
        requests.post(
            webhook_url,
            data=json.dumps({"content": text}),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
    except Exception:
        pass
