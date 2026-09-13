#!/usr/bin/env python3
"""Minimal EasyLeadz callback receiver.

Run this behind HTTPS (reverse proxy / hosting platform). Results are written to
`data/private/`, which is git-ignored, so phone numbers and personal emails are
not committed to the public repository.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = os.environ.get("EASYLEADZ_WEBHOOK_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("EASYLEADZ_WEBHOOK_PORT", "8080")))
TOKEN = os.environ.get("EASYLEADZ_WEBHOOK_TOKEN", "").strip()
STORE = Path(os.environ.get("EASYLEADZ_PRIVATE_STORE", "data/private/easyleadz_results.jsonl"))
MAX_BODY = 1024 * 1024


def _redacted_summary(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data if isinstance(data, dict) else {}
    return {
        "request_id": data.get("request_id", ""),
        "has_phone1": bool(data.get("phone1")),
        "has_phone2": bool(data.get("phone2")),
        "email_count": len(data.get("email") or []) if isinstance(data.get("email"), list) else 0,
        "error": payload.get("error", "") if isinstance(payload, dict) else "",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "DoHSS-EasyLeadz-Webhook/1.0"

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, {"ok": True})
        return self._json(404, {"ok": False})

    def do_POST(self):
        path = urlparse(self.path).path
        expected = f"/easyleadz/{TOKEN}" if TOKEN else ""
        if not expected or path != expected:
            return self._json(404, {"ok": False})

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad content length"})
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"ok": False, "error": "invalid body size"})

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"ok": False, "error": "invalid json"})

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("request_id"):
            return self._json(400, {"ok": False, "error": "missing request_id"})

        STORE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        with STORE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        print("EasyLeadz callback:", json.dumps(_redacted_summary(payload), ensure_ascii=False), flush=True)
        return self._json(200, {"ok": True})

    def log_message(self, fmt, *args):
        # Keep hosting logs concise and avoid echoing callback paths/tokens.
        return


def main() -> None:
    if len(TOKEN) < 24:
        raise SystemExit("Set EASYLEADZ_WEBHOOK_TOKEN to a strong random value (24+ chars)")
    STORE.parent.mkdir(parents=True, exist_ok=True)
    print(f"Listening on {HOST}:{PORT}; health=/health; private store={STORE}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
