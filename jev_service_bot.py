#!/usr/bin/env python3
"""
Service bot that uses only Jev (TypeSafe AI) through OpenRouter.

No LLM generates text here. Jev returns typed decisions with probabilities,
plain Python turns them into canned replies, clarifying questions or a handoff.

    export OPENROUTER_API_KEY=...
    python jev_service_bot.py            # live, calls OpenRouter
    python jev_service_bot.py --mock     # offline, keyword stub instead of Jev
    python jev_service_bot.py --debug    # print every decision Jev returns

Standard library only.
"""

import json
import os
import re
import sys
import time
import urllib.request

URL = "https://openrouter.ai/api/v1/systemone"
MODEL = "jev-latest"

# Thresholds are product decisions, not model settings. Tune them on real traffic.
T_SCOPE = 0.50     # below: not a service request at all
T_HUMAN = 0.70     # above: hand over to a person
T_CHOICE = 0.70    # top option must reach this probability ...
T_MARGIN = 0.20    # ... and lead the runner-up by this much

INTENTS = {
    "track_order": "Where is my parcel, delivery status, delivery date",
    "return_item": "Send something back, refund, damaged or wrong item",
    "invoice": "Invoice copy, wrong amount, payment question",
    "account": "Change address, email, password or other account data",
}

ID_KINDS = {
    "order_number": "Identifies a single purchase",
    "customer_number": "Identifies the customer account",
    "invoice_number": "Identifies a billing document",
    "tracking_number": "Identifies a parcel at the carrier",
}

# Which identifiers each intent can work with.
ACCEPTS = {
    "track_order": ["order_number", "tracking_number"],
    "return_item": ["order_number"],
    "invoice": ["invoice_number", "order_number"],
    "account": ["customer_number"],
}

# Second, narrower question per intent.
DETAIL = {
    "return_item": {
        "instructions": "Why does the customer want to return the item?",
        "criteria": {
            "damaged": "Arrived broken or defective",
            "wrong_item": "Different product, size or colour than ordered",
            "changed_mind": "No defect, customer no longer wants it",
            "unknown": "No reason given yet",
        },
    },
    "invoice": {
        "instructions": "What does the customer need regarding the invoice?",
        "criteria": {
            "copy": "Wants the invoice sent again",
            "wrong_amount": "Disputes the amount or a line item",
            "payment": "Question about payment method or due date",
        },
    },
    "account": {
        "instructions": "Which account data should change?",
        "criteria": {
            "address": "Delivery or billing address",
            "email": "Email address",
            "password": "Password or login problem",
        },
    },
}

REPLIES = {
    "out_of_scope": "I can help with orders, returns, invoices and account data. "
                    "For anything else please use the contact form.",
    "handoff": "I am passing you to a colleague. Your conversation so far is attached.",
    "track_order": "Order {id}: handed to the carrier, delivery expected tomorrow.",
    "return_item/damaged": "Sorry about that. Return label for order {id} is on its way, "
                           "refund follows on receipt. No return shipping costs.",
    "return_item/wrong_item": "Return label for order {id} is on its way. "
                              "Do you want the correct item or a refund?",
    "return_item/changed_mind": "Return label for order {id} is on its way. "
                                "Refund follows on receipt, minus return shipping.",
    "return_item/unknown": "What is the reason for returning order {id}?",
    "invoice/copy": "I have sent the invoice for {id} to the email address on file.",
    "invoice/wrong_amount": "I opened a billing review for {id}. You will hear back within two days.",
    "invoice/payment": "Payment details for {id} are in your account under Billing.",
    "account/address": "You can change the address for customer {id} under Account, Addresses.",
    "account/email": "I sent a confirmation link to the current email of customer {id}.",
    "account/password": "I sent a password reset link to the email of customer {id}.",
}


def label(key):
    return key.replace("_", " ")


def call_jev(state, questions):
    """POST state + typed questions, get typed answers back."""
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def mock_jev(state, questions):
    """Keyword stub so the control flow can be tried without an API key."""
    text = " ".join(m["text"].lower() for m in state["conversation"] if m["role"] == "customer")
    last = state["conversation"][-1]["text"].lower()
    words = {
        "track_order": ["where is", "parcel", "delivery date"],
        "return_item": ["return", "send back", "refund", "broken", "damaged"],
        "invoice": ["invoice", "charged", "bill"],
        "account": ["address", "email", "password", "moved"],
        "order_number": ["order number", "order no", "my order"],
        "customer_number": ["customer number", "customer no"],
        "invoice_number": ["invoice number", "invoice no"],
        "tracking_number": ["tracking"],
        "damaged": ["broken", "damaged", "defect"],
        "wrong_item": ["wrong"],
        "changed_mind": ["don't want", "changed my mind"],
        "copy": ["copy", "again"], "wrong_amount": ["twice", "too much"],
        "address": ["address", "moved"], "email": ["email"], "password": ["password"],
    }
    answers = {}
    for name, q in questions.items():
        if q["type"] == "noul":
            if name == "in_scope":
                p = 0.05 if re.search(r"python|poem|code|recipe", last) else 0.95
            else:
                p = 0.9 if re.search(r"human|agent|ridiculous|third time", last) else 0.05
            answers[name] = {"type": "noul", "noul": p}
            continue
        keys = list(q["criteria"])
        hits = {k: sum(w in text for w in words.get(k, [])) for k in keys}
        total = sum(hits.values())
        probs = {k: (hits[k] / total if total else 1 / len(keys)) for k in keys}
        top = max(probs, key=probs.get)
        answers[name] = {"type": "choice", "choice": top,
                         "confidence": probs[top], "probabilities": probs}
    return {"answers": answers, "usage": {"input_tokens": 0, "cost": 0.0}}


def ranked(answer):
    """Options sorted by probability, highest first."""
    probs = answer.get("probabilities") or {answer["choice"]: answer.get("confidence", 1.0)}
    return sorted(probs.items(), key=lambda kv: kv[1], reverse=True)


def is_clear(answer):
    r = ranked(answer)
    second = r[1][1] if len(r) > 1 else 0.0
    return r[0][1] >= T_CHOICE and r[0][1] - second >= T_MARGIN


def either_or(answer):
    r = ranked(answer)
    return f"{label(r[0][0])} or {label(r[1][0])}"


class Bot:
    def __init__(self, decide, debug=False):
        self.decide = decide
        self.debug = debug
        self.state = {"conversation": [], "number": None}

    def ask(self, questions):
        t0 = time.perf_counter()
        result = self.decide(self.state, questions)
        if self.debug:
            ms = (time.perf_counter() - t0) * 1000
            print(f"    [{ms:.0f} ms, usage {result.get('usage')}]")
            for name, a in result["answers"].items():
                shown = a["noul"] if a["type"] == "noul" else ranked(a)
                print(f"    {name}: {shown}")
        return result["answers"]

    def turn(self, text):
        self.state["conversation"].append({"role": "customer", "text": text})
        found = re.findall(r"\b\d{6,12}\b", text)      # finding digits needs no model
        if found:
            self.state["number"] = found[-1]
        reply = self.route()
        self.state["conversation"].append({"role": "bot", "text": reply})
        return reply

    def route(self):
        number = self.state["number"]
        questions = {
            "in_scope": {"type": "noul", "instructions":
                         "Is the latest customer message a customer service matter for an online shop?"},
            "wants_human": {"type": "noul", "instructions":
                            "Is the customer asking for a human or clearly angry?"},
            "intent": {"type": "choice", "instructions":
                       "What does the customer want?", "criteria": INTENTS},
        }
        if number:
            questions["id_kind"] = {"type": "choice", "instructions":
                                    f"What kind of identifier is {number} in this conversation?",
                                    "criteria": ID_KINDS}
        a = self.ask(questions)                         # all questions in one call

        if a["in_scope"]["noul"] < T_SCOPE:
            return REPLIES["out_of_scope"]
        if a["wants_human"]["noul"] > T_HUMAN:
            return REPLIES["handoff"]
        if not is_clear(a["intent"]):
            return f"Is this about {either_or(a['intent'])}?"

        intent = a["intent"]["choice"]
        accepted = ACCEPTS[intent]
        wanted = " or ".join(label(k) for k in accepted)
        if not number:
            return f"Please give me your {wanted}."
        if not is_clear(a["id_kind"]):
            return f"Is {number} your {either_or(a['id_kind'])}?"
        if a["id_kind"]["choice"] not in accepted:
            return (f"I read {number} as your {label(a['id_kind']['choice'])}. "
                    f"For this I need your {wanted}.")

        key = intent
        if intent in DETAIL:
            d = self.ask({"detail": {"type": "choice", **DETAIL[intent]}})["detail"]
            if not is_clear(d):
                return f"To be sure: {either_or(d)}?"
            key = f"{intent}/{d['choice']}"
        return self.reply(key, number, a["id_kind"]["choice"])

    def reply(self, key, number, id_kind):
        """Final answer for a fully understood request. Override to use real data."""
        return REPLIES[key].format(id=number)


def main():
    mock = "--mock" in sys.argv
    if not mock and "OPENROUTER_API_KEY" not in os.environ:
        sys.exit("Set OPENROUTER_API_KEY or run with --mock")
    bot = Bot(mock_jev if mock else call_jev, debug="--debug" in sys.argv)
    print("Service bot (Jev only). Empty line to quit.")
    while True:
        try:
            text = input("you> ").strip()
        except EOFError:
            break
        if not text:
            break
        print("bot>", bot.turn(text))


if __name__ == "__main__":
    main()
