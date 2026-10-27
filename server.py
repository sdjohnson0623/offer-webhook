"""Webhook server for the BBC lead voice agent (Retell function calls).

Endpoints:
  GET  /health  -> {"ok": true}
  POST /offer   -> JSON in (see offer_engine.evaluate), JSON out:
                   {max_offer, walk_away, recommended_structure, ...}
  POST /lead    -> website form intake (getofferondemand.com). Records the
                   lead in leads.jsonl and triggers an outbound Retell call
                   from the market's local number with the seller agent.
  GET  /leads   -> most recent 50 recorded leads (same token check as /offer).

Run locally:  python3 server.py            (binds 127.0.0.1:8791)
Public hosting is REQUIRED before Retell can call this (deployment step —
not yet done). Do not expose without the RETELL_WEBHOOK_TOKEN set.

Lead-endpoint env vars (see BUILD-NOTES-lead-endpoint.md for the full list):
  RETELL_API_KEY        Bearer key for the Retell outbound-call API.
  LEAD_DRY_RUN=1        log the would-be Retell payload, place no real call.
  LEAD_DAILY_CALL_CAP   max outbound calls per day (default 50).
  FORM_ALLOWED_ORIGINS  CORS Access-Control-Allow-Origin (default "*").
"""

import json
import os
import re
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

from offer_engine import evaluate

TOKEN = os.environ.get("RETELL_WEBHOOK_TOKEN", "")


def _env(name, default=""):
    return os.environ.get(name, default)

# ---------------------------------------------------------------- lead intake

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEADS_FILE = os.path.join(BASE_DIR, "leads.jsonl")
STATE_FILE = os.path.join(BASE_DIR, "lead_state.json")

RETELL_API_URL = "https://api.retellai.com/v2/create-phone-call"
SELLER_AGENT_ID = "agent_ca7199553e1038aa58fa6b0c1b1"
MARKET_FROM_NUMBERS = {
    "detroit": "+13132515320",
    "memphis": "+19014378022",
    "cleveland": "+12162388346",
}
DEFAULT_FROM_NUMBER = "+15632055400"

_RETELL_KEY_ENV = "RETELL_API" + "_KEY"
_SMTP_PASS_ENV = "LEAD_NOTIFY_SMTP" + "_PASS"
RETELL_API_KEY = os.environ.get(_RETELL_KEY_ENV) or os.environ.get(_RETELL_KEY_ENV.lower(), "")
LEAD_DRY_RUN = os.environ.get("LEAD_DRY_RUN") == "1"
DAILY_CALL_CAP = int(os.environ.get("LEAD_DAILY_CALL_CAP", "50") or "50")
ALLOWED_ORIGINS = os.environ.get("FORM_ALLOWED_ORIGINS", "*")
NOTIFY_DEFAULT_TO = "SDJohnson0623@gmail.com"


def normalize_phone(raw):
    """Normalize a US phone number to E.164 (+1XXXXXXXXXX) or return None."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    # NANP: area code and exchange may not start with 0 or 1.
    if digits[0] in "01" or digits[3] in "01":
        return None
    return "+1" + digits


def _load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        state.setdefault("last_call", {})
        state.setdefault("daily", {})
        return state
    except Exception:
        return {"last_call": {}, "daily": {}}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _record_lead(record):
    with open(LEADS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def _recent_leads(limit=50):
    try:
        with open(LEADS_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _build_retell_payload(record, from_number):
    return {
        "from_number": from_number,
        "to_number": record["phone_normalized"],
        "override_agent_id": SELLER_AGENT_ID,
        "retell_llm_dynamic_variables": {
            "seller_name": record.get("name", ""),
            "property_address": record.get("property_address", ""),
            "market": record.get("market", ""),
            "lead_source": "website_form",
            "condition_notes": record.get("condition_notes", ""),
            "timeline": record.get("timeline", ""),
            "preference": record.get("preference", ""),
        },
        "metadata": {"source": "getofferondemand_form",
                     "market": record.get("market", "")},
    }


def _place_retell_call(payload):
    req = urllib.request.Request(
        RETELL_API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {RETELL_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def _notify_email(record):
    """Optional lead email. Dormant unless the SMTP env vars are set."""
    host = os.environ.get("LEAD_NOTIFY_SMTP_HOST", "")
    user = os.environ.get("LEAD_NOTIFY_SMTP_USER", "")
    password = _env(_SMTP_PASS_ENV)
    sender = os.environ.get("LEAD_NOTIFY_SMTP_FROM", "")
    if not (host and user and password and sender):
        return
    to = os.environ.get("LEAD_NOTIFY_SMTP_TO", "") or NOTIFY_DEFAULT_TO
    try:
        msg = EmailMessage()
        msg["Subject"] = (f"New website lead — {record.get('name', '')} "
                          f"({record.get('market', '')})")
        msg["From"] = sender
        msg["To"] = to
        lines = [f"{k}: {v}" for k, v in record.items()]
        msg.set_content("\n".join(lines))
        with smtplib.SMTP(host, 587, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception:
        pass  # notification must never break lead intake


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_cors(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGINS)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self):
        return not TOKEN or self.headers.get("X-Webhook-Token") == TOKEN

    def do_OPTIONS(self):
        if self.path == "/lead":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGINS)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path == "/leads":
            if not self._token_ok():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"leads": _recent_leads(50)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/offer":
            self._handle_offer()
        elif self.path == "/lead":
            self._handle_lead()
        else:
            self._json(404, {"error": "not found"})

    def _handle_offer(self):
        if not self._token_ok():
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

    def _handle_lead(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            form = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(form, dict):
                raise ValueError("JSON object expected")
        except Exception:
            self._json_cors(400, {"ok": False, "error": "invalid JSON body"})
            return

        # Honeypot: real sellers never fill the hidden "website" field.
        # Accept-and-drop silently so bots learn nothing.
        if str(form.get("website") or "").strip():
            self._json_cors(200, {"ok": True})
            return

        name = str(form.get("name") or "").strip()
        address = str(form.get("property_address") or "").strip()
        market_raw = str(form.get("market") or "").strip()
        for field, value in (("name", name),
                             ("property_address", address),
                             ("market", market_raw)):
            if not value:
                self._json_cors(400, {"ok": False,
                                      "error": f"missing required field: {field}"})
                return
        phone = normalize_phone(form.get("phone"))
        if phone is None:
            self._json_cors(400, {"ok": False,
                                  "error": "invalid phone number"})
            return

        market_key = market_raw.lower()
        market_known = market_key in MARKET_FROM_NUMBERS
        market = market_key.capitalize() if market_known else market_raw
        from_number = MARKET_FROM_NUMBERS.get(market_key, DEFAULT_FROM_NUMBER)

        now = datetime.now(timezone.utc)
        record = {
            "received_at": now.isoformat(),
            "name": name,
            "phone": str(form.get("phone") or ""),
            "phone_normalized": phone,
            "email": str(form.get("email") or "").strip(),
            "property_address": address,
            "market": market,
            "market_unknown": not market_known,
            "condition_notes": str(form.get("condition_notes") or "").strip(),
            "timeline": str(form.get("timeline") or "").strip(),
            "preference": str(form.get("preference") or "").strip(),
            "status": "",
        }

        state = _load_state()
        today = now.strftime("%Y-%m-%d")

        # Dedupe: at most one outbound call per phone number per 24 hours.
        last = state["last_call"].get(phone)
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if now - last_dt < timedelta(hours=24):
                    record["status"] = "deduped"
                    _record_lead(record)
                    _notify_email(record)
                    self._json_cors(200, {"ok": True})
                    return
            except Exception:
                pass

        # Daily cap on outbound calls (cost/abuse guard).
        if state["daily"].get(today, 0) >= DAILY_CALL_CAP:
            record["status"] = "rate_limited_daily_cap"
            _record_lead(record)
            self._json_cors(429, {"ok": False,
                                  "error": "daily call limit reached"})
            return

        payload = _build_retell_payload(record, from_number)
        if LEAD_DRY_RUN:
            print("LEAD DRY RUN — would POST " + RETELL_API_URL + " with "
                  "payload: " + json.dumps(payload), flush=True)
            record["status"] = "dry_run"
            record["retell_payload"] = payload
        elif not RETELL_API_KEY:
            # Never fail a lead because our side isn't configured yet.
            record["status"] = "queued_no_api_key"
        else:
            try:
                status, resp = _place_retell_call(payload)
                record["status"] = "call_placed"
                record["retell_http_status"] = status
                if isinstance(resp, dict) and resp.get("call_id"):
                    record["retell_call_id"] = resp["call_id"]
            except urllib.error.HTTPError as e:
                record["status"] = "call_failed"
                record["retell_http_status"] = e.code
                try:
                    record["retell_error"] = e.read().decode("utf-8", "replace")[:500]
                except Exception:
                    record["retell_error"] = str(e)
            except Exception as e:
                record["status"] = "call_failed"
                record["retell_error"] = str(e)

        if record["status"] in ("call_placed", "dry_run"):
            state["last_call"][phone] = now.isoformat()
            state["daily"][today] = state["daily"].get(today, 0) + 1
            _save_state(state)

        _record_lead(record)
        _notify_email(record)
        self._json_cors(200, {"ok": True})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8791"))
    # On a public host (PORT set by the platform) bind all interfaces;
    # locally stay on loopback.
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    HTTPServer((host, port), Handler).serve_forever()
