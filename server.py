#!/usr/bin/env python3
"""
Web front end for the Jev service bot.

Serves a small chat UI and keeps one Bot per browser session in memory.
Replies are filled from the fictional shop in shop_data.json.

    export OPENROUTER_API_KEY=...
    python server.py              # live, calls OpenRouter
    python server.py --mock       # offline, keyword stub instead of Jev

Standard library only.
"""

import json
import os
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jev_service_bot import (Bot, REPLIES, T_CHOICE, T_HUMAN, T_MARGIN, T_SCOPE,
                             call_jev, label, mock_jev, ranked)

HERE = Path(__file__).parent
DATA = json.loads((HERE / "shop_data.json").read_text())
INDEX = (HERE / "static" / "index.html").read_bytes()

PORT = int(os.environ.get("PORT", "8000"))
MAX_TEXT = 400            # characters per message
MAX_TURNS = 20            # customer messages per conversation
SESSION_TTL = 30 * 60     # seconds without activity
MAX_SESSIONS = 2000
RATE_PER_MIN = 15         # messages per client IP
DAILY_CALLS = 5000        # Jev calls per day over all visitors

ORDERS, INVOICES, CUSTOMERS = DATA["orders"], DATA["invoices"], DATA["customers"]


def mask(email):
    name, domain = email.split("@")
    return f"{name[0]}***@{domain}"


def lookup(number, id_kind):
    """Resolve a number to (order_no, order, customer_no, customer) or None."""
    order_no = customer_no = None
    if id_kind == "order_number" and number in ORDERS:
        order_no = number
    elif id_kind == "tracking_number":
        order_no = next((k for k, o in ORDERS.items() if o["tracking"] == number), None)
    elif id_kind == "invoice_number" and number in INVOICES:
        order_no = INVOICES[number]["order"]
    elif id_kind == "customer_number" and number in CUSTOMERS:
        customer_no = number
    if order_no:
        customer_no = ORDERS[order_no]["customer"]
    if not customer_no:
        return None
    return order_no, ORDERS.get(order_no), customer_no, CUSTOMERS[customer_no]


class ShopBot(Bot):
    """Bot that records Jev's decisions and answers from the example data."""

    def __init__(self, decide):
        super().__init__(decide)
        self.trace = []
        self.turns = 0
        self.last_used = time.time()
        self.lock = threading.Lock()

    def ask(self, questions):
        t0 = time.perf_counter()
        result = self.decide(self.state, questions)
        ms = round((time.perf_counter() - t0) * 1000)
        decisions = []
        for name, a in result["answers"].items():
            d = {"name": name, "type": a["type"], "question": questions[name]["instructions"]}
            if a["type"] == "noul":
                d["p"] = a["noul"]
                d["threshold"] = T_SCOPE if name == "in_scope" else T_HUMAN
            else:
                d["options"] = [[label(k), p] for k, p in ranked(a)]
            decisions.append(d)
        self.trace.append({"ms": ms, "cost": (result.get("usage") or {}).get("cost", 0),
                           "decisions": decisions})
        return result["answers"]

    def reply(self, key, number, id_kind):
        found = lookup(number, id_kind)
        if not found:
            return (f"I cannot find {label(id_kind)} {number} in the demo shop. "
                    "Please pick a number from the example data.")
        order_no, order, customer_no, customer = found
        first = customer["name"].split()[0]

        if key == "track_order":
            if order["status"] == "delivered":
                return (f"Order {order_no} ({order['item']}) was delivered by "
                        f"{order['carrier']} on {order['date']}.")
            if order["status"] == "shipped":
                return (f"Order {order_no} ({order['item']}) is on its way with {order['carrier']}, "
                        f"tracking {order['tracking']}. Expected {order['eta']}.")
            return f"Order {order_no} ({order['item']}) is being packed. Expected {order['eta']}."

        if key.startswith("return_item/"):
            return REPLIES[key].format(id=f"{order_no} ({order['item']})")

        if key.startswith("invoice/"):
            inv_no = order["invoice"]
            inv = INVOICES[inv_no]
            if key == "invoice/copy":
                return (f"I have sent invoice {inv_no} ({inv['amount']} EUR, {order['item']}) "
                        f"to {mask(customer['email'])}.")
            if key == "invoice/wrong_amount":
                return (f"I opened a billing review for invoice {inv_no} ({inv['amount']} EUR, "
                        f"{order['item']}). You will hear back within two days.")
            return f"Invoice {inv_no} over {inv['amount']} EUR: {inv['state']}."

        if key == "account/address":
            return (f"Hi {first}, your current address is in {customer['city']}. "
                    "You can change it under Account, Addresses.")
        if key == "account/email":
            return f"Hi {first}, I sent a confirmation link to {mask(customer['email'])}."
        if key == "account/password":
            return f"Hi {first}, I sent a password reset link to {mask(customer['email'])}."
        return REPLIES[key].format(id=number)


class Limits:
    def __init__(self):
        self.lock = threading.Lock()
        self.hits = defaultdict(deque)
        self.day = None
        self.calls = 0

    def allow(self, ip):
        now = time.time()
        with self.lock:
            q = self.hits[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                return "Slow down a little, please try again in a minute."
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.calls = today, 0
            if self.calls >= DAILY_CALLS:
                return "The demo has reached its daily limit. Please come back tomorrow."
            q.append(now)
            self.calls += 2                     # a turn costs at most two Jev calls
            if len(self.hits) > 10000:
                self.hits.clear()
        return None


DECIDE = mock_jev if "--mock" in sys.argv else call_jev
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
LIMITS = Limits()


def get_bot(sid):
    now = time.time()
    with SESSIONS_LOCK:
        for k in [k for k, b in SESSIONS.items() if now - b.last_used > SESSION_TTL]:
            del SESSIONS[k]
        bot = SESSIONS.get(sid) if sid else None
        if bot is None:
            if len(SESSIONS) >= MAX_SESSIONS:
                del SESSIONS[min(SESSIONS, key=lambda k: SESSIONS[k].last_used)]
            sid = secrets.token_urlsafe(16)
            bot = SESSIONS[sid] = ShopBot(DECIDE)
        bot.last_used = now
    return sid, bot


def examples():
    orders = []
    for no, o in ORDERS.items():
        orders.append({"order": no, "item": o["item"], "status": o["status"],
                       "tracking": o["tracking"], "invoice": o["invoice"],
                       "amount": INVOICES[o["invoice"]]["amount"],
                       "customer": o["customer"], "name": CUSTOMERS[o["customer"]]["name"]})
    return {"shop": DATA["shop"], "orders": orders, "starters": DATA["starters"],
            "thresholds": {"choice": T_CHOICE, "margin": T_MARGIN},
            "mock": DECIDE is mock_jev}


class Handler(BaseHTTPRequestHandler):
    server_version = "jev-demo"

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send(200, INDEX, "text/html; charset=utf-8")
        elif path == "/api/examples":
            self.send(200, examples())
        elif path == "/healthz":
            self.send(200, {"ok": True})
        else:
            self.send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/api/chat":
            return self.send(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                return self.send(413, {"error": "Message too long."})
            body = json.loads(self.rfile.read(length))
            text = " ".join(str(body.get("text", "")).split())
            sid = body.get("session")
        except (ValueError, AttributeError):
            return self.send(400, {"error": "Bad request."})
        if not text:
            return self.send(400, {"error": "Please type a message."})
        if len(text) > MAX_TEXT:
            return self.send(400, {"error": f"Please keep it under {MAX_TEXT} characters."})

        ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
        blocked = LIMITS.allow(ip)
        if blocked:
            return self.send(429, {"error": blocked})

        sid, bot = get_bot(sid if isinstance(sid, str) else None)
        with bot.lock:
            if bot.turns >= MAX_TURNS:
                return self.send(400, {"error": "This conversation is long enough. "
                                                "Please start a new one.", "session": sid})
            bot.trace = []
            before = len(bot.state["conversation"])
            try:
                reply = bot.turn(text)
            except Exception as e:                      # Jev unreachable or unexpected answer
                del bot.state["conversation"][before:]
                print(f"turn failed: {e!r}", file=sys.stderr, flush=True)
                return self.send(502, {"error": "Jev did not answer. Please try again.",
                                       "session": sid})
            bot.turns += 1
            self.send(200, {"session": sid, "reply": reply, "calls": bot.trace,
                            "turns_left": MAX_TURNS - bot.turns})

    def log_message(self, fmt, *args):
        pass


def main():
    if DECIDE is call_jev and "OPENROUTER_API_KEY" not in os.environ:
        sys.exit("Set OPENROUTER_API_KEY or run with --mock")
    print(f"Jev demo on :{PORT} ({'mock' if DECIDE is mock_jev else 'live'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
