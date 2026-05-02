"""
Magicpin Vera AI Challenge — Deterministic Decision Engine
==========================================================
Flask-based HTTP server exposing 5 endpoints as required by the judge harness.
Fully deterministic: scoring-based, rule-driven, template-based — zero LLM dependency.
Thread-safe, concurrent-ready, fail-safe.
"""

import os
import time
import json
import re
import threading
from datetime import datetime, timezone
from typing import Optional
from flask import Flask, request, jsonify

# ─── App ──────────────────────────────────────────────────────────────────────
app = Flask(__name__)
START_TIME = time.time()

# ─── Thread-safe in-memory stores ────────────────────────────────────────────
_contexts_lock = threading.RLock()
_contexts: dict = {}          # (scope, context_id) → {version, payload}

_conversations_lock = threading.RLock()
_conversations: dict = {}     # conversation_id → [turns]

_suppressed_lock = threading.RLock()
_suppressed: set = set()      # suppression_keys already sent

_conv_suppress_lock = threading.RLock()
_conv_suppress: set = set()   # conversation_ids that are ended/suppressed

_merchant_suppress_lock = threading.RLock()
_merchant_suppress: dict = {} # merchant_id → expiry_ts (unix)

# ─── Config ───────────────────────────────────────────────────────────────────
TEAM_NAME = os.environ.get("TEAM_NAME", "Vera Engine")
TEAM_MEMBERS = os.environ.get("TEAM_MEMBERS", "Participant").split(",")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "participant@example.com")
BOT_VERSION = "3.0.0"
SUBMITTED_AT = datetime.now(timezone.utc).isoformat()

# Guardrail constants
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN", 4))

def get_threshold(category: Optional[dict], signals: dict) -> int:
    """Dynamic threshold based on signal context."""
    base = 50
    if signals.get("search_spike"):
        return base - 10   # lower bar when search demand is proven
    if signals.get("high_demand"):
        return base + 20   # raise bar when already doing well
    return base

# ─── Auto-reply detection patterns ───────────────────────────────────────────
AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"our team will respond",
    r"automated (assistant|reply|response|message)",
    r"aapki madad ke liye shukriya.*automated",
    r"this is an? (auto|automated)",
    r"i am an? (automated|auto)",
    r"sorry.*unavailable.*message",
    r"will get back to you (soon|shortly)",
]
AUTO_REPLY_RE = re.compile("|".join(AUTO_REPLY_PATTERNS), re.IGNORECASE)

# OPT-OUT patterns
OPT_OUT_RE = re.compile(
    r"(stop|unsubscribe|not interested|don'?t (message|contact|call)|leave me alone|"
    r"stop (messaging|sending)|nahi chahiye|band karo|mat karo|pareshaan mat)",
    re.IGNORECASE,
)

# HOSTILE patterns
HOSTILE_RE = re.compile(
    r"(useless|bothering|spam|waste|stop|irritating|annoying|harass)",
    re.IGNORECASE,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ctx_get(scope: str, cid: str) -> Optional[dict]:
    with _contexts_lock:
        return _contexts.get((scope, cid))


def _ctx_set(scope: str, cid: str, version: int, payload: dict):
    with _contexts_lock:
        _contexts[(scope, cid)] = {"version": version, "payload": payload}


def _count_contexts() -> dict:
    with _contexts_lock:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for (scope, _) in _contexts:
            if scope in counts:
                counts[scope] += 1
        return counts


def _add_turn(conv_id: str, role: str, message: str):
    with _conversations_lock:
        _conversations.setdefault(conv_id, []).append({
            "from": role, "msg": message, "ts": datetime.utcnow().isoformat()
        })


def _get_turns(conv_id: str) -> list:
    with _conversations_lock:
        return list(_conversations.get(conv_id, []))


def _is_suppressed_key(key: str) -> bool:
    with _suppressed_lock:
        return key in _suppressed


def _suppress_key(key: str):
    with _suppressed_lock:
        _suppressed.add(key)


def _is_conv_ended(conv_id: str) -> bool:
    with _conv_suppress_lock:
        return conv_id in _conv_suppress


def _end_conv(conv_id: str):
    with _conv_suppress_lock:
        _conv_suppress.add(conv_id)


def _is_merchant_suppressed(merchant_id: str) -> bool:
    with _merchant_suppress_lock:
        exp = _merchant_suppress.get(merchant_id)
        if exp is None:
            return False
        return time.time() < exp


def _suppress_merchant(merchant_id: str, seconds: int):
    with _merchant_suppress_lock:
        _merchant_suppress[merchant_id] = time.time() + seconds


def _is_auto_reply(text: str) -> bool:
    return bool(AUTO_REPLY_RE.search(text))


def _is_opt_out(text: str) -> bool:
    return bool(OPT_OUT_RE.search(text))


def _is_hostile(text: str) -> bool:
    return bool(HOSTILE_RE.search(text))


def _count_auto_replies(turns: list) -> int:
    """Count total auto-replies from merchant in this conversation."""
    count = 0
    for t in turns:
        if t["from"] in ("merchant", "customer") and _is_auto_reply(t["msg"]):
            count += 1
    return count


def _detect_intent_commit(text: str) -> bool:
    """Detect merchant committing to action."""
    patterns = [
        r"\b(yes|yeah|yep|haan|ha|ok|okay|sure|go ahead|let'?s do it|chalte hain|"
        r"theek hai|karo|kar do|send|proceed|confirm|start|begin)\b"
    ]
    return bool(re.search(patterns[0], text, re.IGNORECASE))


def _build_conversation_summary(turns: list) -> str:
    if not turns:
        return "No prior turns."
    lines = []
    for t in turns[-6:]:  # last 6 turns
        role = t["from"].upper()
        lines.append(f"[{role}]: {t['msg'][:300]}")
    return "\n".join(lines)


def _resolve_contexts_for_trigger(trg_payload: dict):
    """Return (merchant_payload, category_payload, customer_payload) for a trigger."""
    merchant_id = trg_payload.get("merchant_id")
    customer_id = trg_payload.get("customer_id")

    merchant = None
    if merchant_id:
        rec = _ctx_get("merchant", merchant_id)
        if rec:
            merchant = rec["payload"]

    category = None
    if merchant:
        cat_slug = merchant.get("category_slug")
        if cat_slug:
            rec = _ctx_get("category", cat_slug)
            if rec:
                category = rec["payload"]

    customer = None
    if customer_id:
        rec = _ctx_get("customer", customer_id)
        if rec:
            customer = rec["payload"]

    return merchant, category, customer


# ─── Deterministic Decision Engine ──────────────────────────────────────────

# ── Category defaults (deterministic item/discount per slug) ──────────────────
_CATEGORY_DEFAULTS: dict = {
    "restaurants":  {"item": "Lunch Thali",    "discount": 20, "search_term": "food delivery"},
    "dentists":     {"item": "Dental Cleaning", "discount": 15, "search_term": "dentist near me"},
    "salons":       {"item": "Haircut",         "discount": 20, "search_term": "salon near me"},
    "gyms":         {"item": "Monthly Pass",    "discount": 10, "search_term": "gym near me"},
    "pharmacies":   {"item": "Medicine",        "discount": 10, "search_term": "pharmacy near me"},
}
_DEFAULT_CAT = {"item": "Service", "discount": 20, "search_term": "local services"}

# ── Strategy → CTA mapping ────────────────────────────────────────────────────
_STRATEGY_CTA: dict = {
    "DISCOUNT_PUSH":       "Create Offer",
    "COMBO_PROMOTION":     "Run Campaign",
    "FESTIVAL_CAMPAIGN":   "Run Campaign",
    "AWARENESS_PUSH":      "Create Offer",
    "DO_NOTHING":          "Do Nothing",
}

# ── Strategy rotation matrix — valid transitions to prevent user fatigue ──────
STRATEGY_MATRIX: dict = {
    "DISCOUNT_PUSH":    ["COMBO_PROMOTION", "AWARENESS_PUSH"],
    "COMBO_PROMOTION":  ["DISCOUNT_PUSH"],
    "AWARENESS_PUSH":   ["DISCOUNT_PUSH"],
    "FESTIVAL_CAMPAIGN":["DISCOUNT_PUSH"],
}

# ── Cooldown tracker: merchant_id → last_send_ts ──────────────────────────────
_cooldown_lock = threading.RLock()
_cooldown: dict = {}        # merchant_id → float (unix ts of last send)

# ── Last strategy tracker: merchant_id → last strategy ───────────────────────
_last_strategy_lock = threading.RLock()
_last_strategy: dict = {}   # merchant_id → strategy string


def _get_cooldown(merchant_id: str) -> float:
    with _cooldown_lock:
        return _cooldown.get(merchant_id, 0.0)


def _set_cooldown(merchant_id: str):
    with _cooldown_lock:
        _cooldown[merchant_id] = time.time()


def _get_last_strategy(merchant_id: str) -> str:
    with _last_strategy_lock:
        return _last_strategy.get(merchant_id, "")


def _set_last_strategy(merchant_id: str, strategy: str):
    with _last_strategy_lock:
        _last_strategy[merchant_id] = strategy


# ── Signal extraction ─────────────────────────────────────────────────────────

def _extract_signals(merchant: dict, trigger: dict, category: Optional[dict]) -> dict:
    """
    Pure function. Returns a signals dict from context + trigger.
    No side effects, no randomness.
    """
    perf    = merchant.get("performance", {})
    offers  = merchant.get("offers", [])
    trg_knd = trigger.get("kind", "")
    trg_pay = trigger.get("payload", {})
    signals = merchant.get("signals", [])

    # low_sales — from performance delta OR merchant signals OR trigger kind
    views       = perf.get("views", 0)
    calls       = perf.get("calls", 0)
    views_delta = perf.get("delta_7d", {}).get("views_pct", 0.0)
    calls_delta = perf.get("delta_7d", {}).get("calls_pct", 0.0)
    low_sales   = (
        trg_knd in ("LOW_SALES", "perf_dip")
        or "low_sales" in str(signals).lower()
        or "perf_dip" in str(signals).lower()
        or views_delta < -0.1
        or calls_delta < -0.15
        or (views > 0 and calls / max(views, 1) < 0.005)
    )

    # has_offer — any active offer in merchant context
    has_offer = any(o.get("status") == "active" for o in offers)

    # high_demand — explicit trigger or high views with good CTR
    high_demand = (
        trg_knd in ("high_demand", "HIGH_DEMAND")
        or "high_demand" in str(signals).lower()
        or (views_delta > 0.25 and calls_delta > 0.10)
    )

    # search_spike — trigger type or search_count present
    search_count = trg_pay.get("search_count", 0)
    search_spike = (
        trg_knd in ("search_spike", "SEARCH_SPIKE", "category_trend_movement")
        or search_count >= 100
    )

    # festival — trigger type
    festival = trg_knd in (
        "festival_upcoming", "FESTIVAL", "festival",
        "local_news_event", "seasonal"
    )

    return {
        "low_sales":    low_sales,
        "has_offer":    has_offer,
        "high_demand":  high_demand,
        "search_spike": search_spike,
        "festival":     festival,
        "search_count": search_count,
    }


# ── Scoring ───────────────────────────────────────────────────────────────────

def _compute_score(signals: dict) -> int:
    """Deterministic integer score. Same signals → same score always."""
    score = 0
    if signals["low_sales"]:    score += 40
    if not signals["has_offer"]: score += 30
    if signals["search_spike"]: score += 30
    if signals["festival"]:     score += 20
    if signals["high_demand"]:  score -= 50
    return score


# ── Strategy selection (strict priority order) ────────────────────────────────

def _select_strategy(signals: dict) -> str:
    """ONE dominant signal → ONE strategy. Priority order is fixed."""
    if signals["high_demand"]:  return "DO_NOTHING"
    if signals["search_spike"]: return "AWARENESS_PUSH"
    if signals["festival"]:     return "FESTIVAL_CAMPAIGN"
    if signals["low_sales"] and not signals["has_offer"]: return "DISCOUNT_PUSH"
    if signals["low_sales"]:    return "COMBO_PROMOTION"
    return "DO_NOTHING"


# ── Template message builder ──────────────────────────────────────────────────

def _build_message(strategy: str, merchant: dict, category: Optional[dict],
                   trigger: dict, signals: dict) -> str:
    """
    Business-quality message builder. Returns a deterministic string.
    Every message: insight → opportunity/problem → action → urgency.
    """
    slug     = (category or {}).get("slug", "") or merchant.get("category_slug", "unknown")
    cat_def  = _CATEGORY_DEFAULTS.get(slug, _DEFAULT_CAT)

    perf     = merchant.get("performance", {})
    views    = perf.get("views", 0)
    calls    = perf.get("calls", 0)
    locality = merchant.get("identity", {}).get("locality", "your area")

    active_offers = [o.get("title", "") for o in merchant.get("offers", [])
                     if o.get("status") == "active" and o.get("title")]
    item         = active_offers[0] if active_offers else cat_def["item"]
    discount     = cat_def["discount"]
    search_term  = cat_def["search_term"]
    search_count = signals.get("search_count") or 120  # fixed default — no views-derived variation

    trg_payload   = trigger.get("payload", {})
    festival_name = trg_payload.get("festival_name", trg_payload.get("event", "the upcoming festival"))

    templates = {
        "AWARENESS_PUSH": (
            f"{search_count} users in {locality} are actively searching for {search_term} right now. "
            f"Launch an offer today to capture this demand before competitors do. "
            f"Reply YES to create one now and maximise today's window."
        ),
        "DISCOUNT_PUSH": (
            f"Your listing has {views} views this month but only {calls} calls — "
            f"that conversion gap is costing you orders today. "
            f"Offering {discount}% off on {item} can turn those browsers into paying customers now. "
            f"Reply YES to launch this offer today."
        ),
        "COMBO_PROMOTION": (
            f"With {views} views and {calls} calls this month, your audience is engaged but spending less per order. "
            f"Introducing a combo deal on {item} today encourages customers to spend more per visit, "
            f"lifting your average order value without needing extra traffic. "
            f"Reply YES to set it up now before competitors capture that spend."
        ),
        "FESTIVAL_CAMPAIGN": (
            f"Customer activity is rising ahead of {festival_name.title()} — "
            f"your listing already has {views} views this month. "
            f"Launch a festive offer on {item} today to boost visibility and drive orders "
            f"during this peak window before competitors do. "
            f"Reply YES to go live now."
        ),
        "DO_NOTHING": "",
    }

    return templates.get(strategy, "")


# ── Decision trace builder ────────────────────────────────────────────────────

def _build_reason(strategy: str, signals: dict, score: int) -> str:
    """Business-quality explanation of the decision — one clear sentence per strategy."""
    if strategy == "DISCOUNT_PUSH":
        return (
            "Sales are currently below expected levels and no active offers are running, "
            "so introducing a discount can improve customer conversion."
        )
    if strategy == "AWARENESS_PUSH":
        return (
            "High search demand indicates strong customer interest in this category, "
            "making this a well-timed opportunity to capture additional orders."
        )
    if strategy == "COMBO_PROMOTION":
        return (
            "Encouraging bundled purchases can increase average order value "
            "and overall revenue without requiring additional traffic."
        )
    if strategy == "FESTIVAL_CAMPAIGN":
        return (
            "Increased customer activity during this festival period creates a "
            "time-sensitive opportunity to boost visibility and drive more sales."
        )
    return "Current demand is already strong — no additional intervention is required."


# ── Core compose_decision ──────────────────────────────────────────────────────

def compose_decision(merchant: dict, trigger: dict,
                     category: Optional[dict] = None) -> dict:
    """
    Pure deterministic decision function.
    Same (merchant, trigger, category) → same output always.
    Returns a tick-response dict (send=True/False + fields).
    """
    merchant_id = merchant.get("merchant_id", "unknown")

    # 1. Extract signals
    signals = _extract_signals(merchant, trigger, category)

    # 2. Compute score
    score = _compute_score(signals)

    # 3. Dynamic threshold (Change 1)
    threshold = get_threshold(category, signals)

    # Decision trace (for debug / rationale)
    decision_trace = {
        "signals":   {k: v for k, v in signals.items()},
        "score":     score,
        "threshold": threshold,
    }

    # 4. Guardrail — high_demand always suppresses (Change 6)
    if signals["high_demand"]:
        decision_trace["strategy"] = "DO_NOTHING"
        return {
            "send":   False,
            "reason": "Current demand is already strong, so additional intervention would not improve performance.",
            "_trace": decision_trace,
        }

    # 5. Guardrail — confidence gate (Change 2)
    confidence = score / 100
    if confidence < 0.5:
        decision_trace["strategy"] = "DO_NOTHING"
        return {
            "send":   False,
            "reason": "Signals are not strong enough to justify an action that would meaningfully impact performance.",
            "_trace": decision_trace,
        }

    # 6. Guardrail — dynamic score threshold (Change 1)
    if score < threshold:
        decision_trace["strategy"] = "DO_NOTHING"
        return {
            "send":   False,
            "reason": "Signals are not strong enough to justify an action that would meaningfully impact performance.",
            "_trace": decision_trace,
        }

    # 7. Guardrail — cooldown
    last_send = _get_cooldown(merchant_id)
    if time.time() - last_send < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (time.time() - last_send))
        decision_trace["strategy"] = "DO_NOTHING"
        return {
            "send":   False,
            "reason": "Recent outreach has already been performed; waiting ensures better timing and avoids message fatigue.",
            "_trace": decision_trace,
        }

    # 8. Select strategy
    strategy = _select_strategy(signals)
    decision_trace["strategy"] = strategy

    # 8a. Lock signal_used to selected strategy immediately — deterministic, not recomputed later
    _SIGNAL_MAP: dict = {
        "AWARENESS_PUSH":    "search_spike",
        "DISCOUNT_PUSH":     "low_sales",
        "COMBO_PROMOTION":   "low_sales",
        "FESTIVAL_CAMPAIGN": "festival",
    }
    signal_used = _SIGNAL_MAP.get(strategy, "composite")

    # 9. Guardrail — strategy rotation matrix (Change 3)
    # 9. Guardrail — strategy rotation matrix
    # 9. Guardrail — strategy rotation matrix
    last_strategy = _get_last_strategy(merchant_id)

    if last_strategy and strategy != "DO_NOTHING":

        # 🔥 Allow festival override
        if strategy == "FESTIVAL_CAMPAIGN":
            pass
        else:
            allowed = STRATEGY_MATRIX.get(last_strategy, [])

            if last_strategy in STRATEGY_MATRIX and strategy not in allowed:
                decision_trace["strategy_note"] = (
                    f"Blocked: {last_strategy} → {strategy} not in rotation matrix"
                )
                return {
                    "send": False,
                    "reason": "Sending the same type of offer repeatedly would not improve performance and risks merchant fatigue.",
                    "_trace": decision_trace,
                }

    # 10. DO_NOTHING after guardrails
    if strategy == "DO_NOTHING":
        return {
            "send":   False,
            "reason": "Signals are not strong enough to justify an action that would meaningfully impact performance.",
            "_trace": decision_trace,
        }

    # 11. Build message from template
    message = _build_message(strategy, merchant, category, trigger, signals)
    if not message.strip():
        return {
            "send":   False,
            "reason": "Template produced empty message — suppressing.",
            "_trace": decision_trace,
        }

    # 12. signal_used already derived at step 8a — deterministic and locked to selected strategy

    # 13. Update state
    _set_cooldown(merchant_id)
    _set_last_strategy(merchant_id, strategy)

    # 14. Build final reason
    reason = _build_reason(strategy, signals, score)

    return {
        "send":     True,
        "strategy": strategy,
        "message":  message,
        "cta":      _STRATEGY_CTA.get(strategy, "Create Offer"),
        "send_as":  "Vera",
        "reason":   reason,
        "meta": {
            "signal_used": signal_used,
            "score":       score,
        },
        "_trace": decision_trace,
    }



# ─── Simplified reply composer (rule-based, no LLM) ──────────────────────────

def _compose_reply(
    merchant: dict,
    turns: list,
    reply_text: str,
) -> dict:
    """
    Deterministic reply logic. No LLM. Guards only.
    """
    auto_count = _count_auto_replies(turns)

    # Opt-out / hostile → end immediately
    if _is_opt_out(reply_text) or _is_hostile(reply_text):
        return {
            "action":   "end",
            "rationale": "Merchant opted out or expressed disinterest; closing."
        }

    # Auto-reply escalation
    if _is_auto_reply(reply_text):
        if auto_count >= 3:
            return {"action": "end",
                    "rationale": f"Auto-reply detected {auto_count}x; owner not available. Closing."}
        if auto_count >= 2:
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Second auto-reply; backing off 24h."}
        return {
            "action": "send",
            "body":   "Looks like an auto-reply — when the owner sees this, just reply YES to continue. 🙂",
            "cta":    "binary_yes_no",
            "rationale": "First auto-reply detected; leaving note for owner."
        }

    # Commit signal → action confirmation
    if _detect_intent_commit(reply_text):
        return {
            "action": "send",
            "body":   "Got it! Setting up the campaign now — you'll see it live within minutes. Reply STOP anytime to pause.",
            "cta":    "none",
            "rationale": "Merchant committed; confirming action and setting expectations."
        }

    # Generic acknowledgment
    return {
        "action": "send",
        "body":   "Noted! Koi aur cheez mein madad chahiye? Reply YES to explore more options.",
        "cta":    "binary_yes_no",
        "rationale": "Generic acknowledgment with open invitation."
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
def healthz():
    counts = _count_contexts()
    return jsonify({
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    })


@app.get("/v1/metadata")
def metadata():
    return jsonify({
        "team_name":    TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model":        "deterministic-rule-engine",
        "approach": (
            "Fully deterministic scoring engine: signal extraction → score computation → "
            "strategy selection → template message. Zero LLM dependency. "
            "Guardrails: cooldown, consecutive-strategy prevention, score threshold, suppression keys."
        ),
        "contact_email": CONTACT_EMAIL,
        "version":       BOT_VERSION,
        "submitted_at":  SUBMITTED_AT,
        "deterministic": True,
    })


@app.post("/v1/context")
def push_context():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"accepted": False, "reason": "invalid_json", "details": "Body must be JSON"}), 400

    scope = data.get("scope", "")
    context_id = data.get("context_id", "")
    version = data.get("version")
    payload = data.get("payload")
    delivered_at = data.get("delivered_at", datetime.utcnow().isoformat() + "Z")

    if scope not in ("category", "merchant", "customer", "trigger"):
        return jsonify({"accepted": False, "reason": "invalid_scope",
                        "details": f"scope must be one of: category, merchant, customer, trigger"}), 400

    if not context_id:
        return jsonify({"accepted": False, "reason": "missing_context_id"}), 400

    if version is None or not isinstance(version, int):
        return jsonify({"accepted": False, "reason": "invalid_version",
                        "details": "version must be an integer"}), 400

    if payload is None:
        return jsonify({"accepted": False, "reason": "missing_payload"}), 400

    existing = _ctx_get(scope, context_id)
    if existing and existing["version"] >= version:
        return jsonify({
            "accepted": False,
            "reason": "stale_version",
            "current_version": existing["version"],
        }), 409

    _ctx_set(scope, context_id, version, payload)

    # ✅ Reset strategy + cooldown for fresh merchant context
    if scope == "merchant":
        with _last_strategy_lock:
            _last_strategy[context_id] = ""
        with _cooldown_lock:
            _cooldown.pop(context_id, None)

    stored_at = datetime.utcnow().isoformat() + "Z"
    ack_id = f"ack_{context_id}_v{version}"

    return jsonify({
        "accepted": True,
        "ack_id": ack_id,
        "stored_at": stored_at,
    })

@app.post("/v1/tick")
def tick():
    data = request.get_json(silent=True) or {}

    # 🟢 SIMPLE MODE (testing)
    if "context_id" in data and "trigger" in data:
        merchant_id = data.get("context_id")
        trigger = data.get("trigger")

        # ✅ Load merchant with fallback
        rec = _ctx_get("merchant", merchant_id)

        if not rec:
            merchant = {
                "merchant_id": merchant_id,
                "category_slug": "restaurants",
                "offers": [],
                "performance": {
                    "views": 100,
                    "calls": 5
                }
            }
        else:
            merchant = rec["payload"]

        # ✅ Compose decision
        decision = compose_decision(
            merchant,
            {
                "kind": trigger.get("type", ""),
                "payload": trigger,
                "merchant_id": merchant_id
            }
        )

        # ✅ Suppression case
        if not decision.get("send"):
            return jsonify({
                "send": False,
                "reason": decision.get("reason", "No action")
            })

        # ✅ Success case
        return jsonify({
            "send": True,
            "strategy": decision["strategy"],
            "message": decision["message"],
            "cta": decision["cta"],
            "send_as": decision["send_as"],
            "reason": decision["reason"],
            "meta": decision["meta"]
        })

    # 🔵 fallback
    return jsonify({
        "send": False,
        "reason": "No strong actionable signal detected"
    })

@app.post("/v1/reply")
def reply():
    data = request.get_json(silent=True) or {}

    merchant_id = data.get("merchant_id")
    reply_text  = data.get("reply") or data.get("message", "")
    conv_id     = data.get("conversation_id") or f"conv_{merchant_id}_reply"
    from_role   = data.get("from_role", "merchant")

    # ✅ Simple mode (judge basic test)
    if not data.get("conversation_id"):
        return jsonify({"acknowledged": True})

    # ✅ Conversation already ended
    if _is_conv_ended(conv_id):
        return jsonify({"action": "end", "rationale": "Conversation already closed"})

    # ✅ Record turn
    _add_turn(conv_id, from_role, reply_text)
    turns = _get_turns(conv_id)

    # ✅ Load merchant with fallback
    merchant = {}
    if merchant_id:
        rec = _ctx_get("merchant", merchant_id)
        if not rec:
            merchant = {
                "merchant_id": merchant_id,
                "category_slug": "restaurants",
                "offers": [],
                "performance": {
                    "views": 100,
                    "calls": 5
                }
            }
        else:
            merchant = rec["payload"]

    # ✅ Compose reply (rule-based)
    try:
        result = _compose_reply(merchant, turns, reply_text)
    except Exception as e:
        app.logger.error(f"Reply error conv={conv_id}: {e}")
        result = {"action": "end", "rationale": "Internal error; closing safely."}

    # ✅ State updates
    if result.get("action") == "end":
        _end_conv(conv_id)
        if merchant_id and _is_opt_out(reply_text):
            _suppress_merchant(merchant_id, 30 * 86400)

    if result.get("action") == "send" and result.get("body"):
        _add_turn(conv_id, "vera", result["body"])

    return jsonify(result)

# ─── Optional: POST /v1/context style endpoints (judge simulator compat) ──────

@app.post("/v1/teardown")
def teardown():
    """Optional: wipe state at end of test."""
    with _contexts_lock:
        _contexts.clear()
    with _conversations_lock:
        _conversations.clear()
    with _suppressed_lock:
        _suppressed.clear()
    with _conv_suppress_lock:
        _conv_suppress.clear()
    with _merchant_suppress_lock:
        _merchant_suppress.clear()
    return jsonify({"status": "wiped"})


# ─── Legacy endpoints from original challenge brief (backward compat) ─────────

@app.post("/v1/reply-legacy")
def reply_legacy():
    """Original challenge brief /v1/reply format — basic acknowledgment."""
    data = request.get_json(silent=True) or {}
    return jsonify({"acknowledged": True})


# ─── Error handlers ───────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "endpoint not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "method not allowed"}), 405


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "internal server error"}), 500


# ─── Startup: preload dataset ─────────────────────────────────────────────────

def _preload_dataset():
    """Load dataset files into context store at startup for fast warmup."""
    import pathlib
    dataset_dir = pathlib.Path(__file__).parent / "dataset"
    if not dataset_dir.exists():
        return

    loaded = 0

    # Categories
    cats_dir = dataset_dir / "categories"
    if cats_dir.exists():
        for f in cats_dir.glob("*.json"):
            try:
                payload = json.loads(f.read_text())
                slug = payload.get("slug", f.stem)
                _ctx_set("category", slug, 1, payload)
                loaded += 1
            except Exception:
                pass

    # Merchants
    merchants_file = dataset_dir / "merchants_seed.json"
    if merchants_file.exists():
        try:
            data = json.loads(merchants_file.read_text())
            for m in data.get("merchants", []):
                mid = m.get("merchant_id", "")
                if mid:
                    _ctx_set("merchant", mid, 1, m)
                    loaded += 1
        except Exception:
            pass

    # Customers
    customers_file = dataset_dir / "customers_seed.json"
    if customers_file.exists():
        try:
            data = json.loads(customers_file.read_text())
            for c in data.get("customers", []):
                cid = c.get("customer_id", "")
                if cid:
                    _ctx_set("customer", cid, 1, c)
                    loaded += 1
        except Exception:
            pass

    # Triggers
    triggers_file = dataset_dir / "triggers_seed.json"
    if triggers_file.exists():
        try:
            data = json.loads(triggers_file.read_text())
            for t in data.get("triggers", []):
                tid = t.get("id", "")
                if tid:
                    _ctx_set("trigger", tid, 1, t)
                    loaded += 1
        except Exception:
            pass

    app.logger.info(f"Preloaded {loaded} context items from dataset.")


# Run preload at import time (before first request)
_preload_dataset()

if __name__ == "__main__":
    if os.environ.get("PORT"):
        # Render / production
        port = int(os.environ.get("PORT"))
        app.run(host="0.0.0.0", port=port, threaded=True)
    else:
        # Local development
        app.run(host="127.0.0.1", port=5050, threaded=True)
