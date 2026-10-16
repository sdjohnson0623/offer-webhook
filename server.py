"""Webhook server for the BBC lead voice agent (Retell function calls).

Endpoints:
  GET  /health  -> {"ok": true}
  POST /offer   -> JSON in (see offer_engine.evaluate), JSON out:
                   {max_offer, walk_away, recommended_structure, ...}

Run locally:  python3 server.py            (binds 127.0.0.1:8791)
Public hosting is REQUIRED before Retell can call this (deployment step —
not yet done). Do not expose without the RETELL_WEBHOOK_TOKEN set.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from offer_engine import evaluate

TOKEN = os.environ.get("RETELL_WEBHOOK_TOKEN", "")


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/offer":
            self._json(404, {"error": "not found"})
            return
        if TOKEN and self.headers.get("X-Webhook-Token") != TOKEN:
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            # Retell function calls may nest arguments; accept either shape.
            args = payload.get("args") or payload.get("arguments") or payload
            result = evaluate(args)
            self._json(200, result)
        except KeyError as e:
            self._json(400, {"error": f"missing required field: {e}"})
        except Exception as e:  # keep the agent from hanging on a bad payload
            self._json(400, {"error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8791"))
    # On a public host (PORT set by the platform) bind all interfaces;
    # locally stay on loopback.
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    HTTPServer((host, port), Handler).serve_forever()
