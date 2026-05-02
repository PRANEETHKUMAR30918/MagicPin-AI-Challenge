"""
conversation_handlers.py — Multi-turn conversation state management.
Optional module for the Magicpin Vera Challenge.
"""
from dataclasses import dataclass, field
from typing import Optional
import re


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    turns: list = field(default_factory=list)
    status: str = "active"        # active | waiting | ended
    auto_reply_count: int = 0
    intent_stage: str = "qualify" # qualify | action | close
    last_send_body: str = ""


# In-memory states (module-level for stateful operation)
_states: dict[str, ConversationState] = {}


def get_or_create(conv_id: str, merchant_id: str, customer_id: Optional[str] = None) -> ConversationState:
    if conv_id not in _states:
        _states[conv_id] = ConversationState(
            conversation_id=conv_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
        )
    return _states[conv_id]


AUTO_REPLY_RE = re.compile(
    r"thank you for contacting|our team will respond|automated (assistant|reply)|"
    r"i am an? automated|aapki madad ke liye shukriya",
    re.IGNORECASE
)

OPT_OUT_RE = re.compile(
    r"\b(stop|unsubscribe|not interested|don'?t message|nahi chahiye|band karo)\b",
    re.IGNORECASE
)

COMMIT_RE = re.compile(
    r"\b(yes|yeah|haan|ok|okay|sure|go ahead|let'?s do it|chalte hain|theek hai|"
    r"karo|kar do|send it|proceed|confirm|start|great)\b",
    re.IGNORECASE
)


def respond(state: ConversationState, merchant_message: str) -> dict:
    """
    Given conversation state + merchant's latest message, return next action dict.
    Called by bot.py's /v1/reply handler for stateful multi-turn management.
    """
    if state.status == "ended":
        return {"action": "end", "rationale": "Conversation already ended."}

    # Track auto-replies
    if AUTO_REPLY_RE.search(merchant_message):
        state.auto_reply_count += 1
    else:
        state.auto_reply_count = 0

    state.turns.append({"from": "merchant", "msg": merchant_message})

    # Opt-out
    if OPT_OUT_RE.search(merchant_message):
        state.status = "ended"
        return {"action": "end", "rationale": "Merchant opted out. Closing."}

    # Auto-reply escalation
    if AUTO_REPLY_RE.search(merchant_message):
        if state.auto_reply_count >= 3:
            state.status = "ended"
            return {"action": "end", "rationale": "Auto-reply 3× in a row; closing."}
        elif state.auto_reply_count == 2:
            state.status = "waiting"
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Second consecutive auto-reply; backing off 24h."}
        else:
            body = "Looks like an auto-reply 🙂 When the owner sees this, reply YES to continue."
            state.turns.append({"from": "vera", "msg": body})
            return {"action": "send", "body": body, "cta": "binary_yes_no",
                    "rationale": "First auto-reply; leaving note for owner."}

    # Intent transition
    if COMMIT_RE.search(merchant_message) and state.intent_stage == "qualify":
        state.intent_stage = "action"
        body = "Bढ़িया! Proceeding now — drafting the campaign. I'll send the preview in 60 seconds. Reply CONFIRM to go live."
        state.turns.append({"from": "vera", "msg": body})
        return {
            "action": "send",
            "body": body,
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant committed; switched to action mode immediately.",
        }

    # Repeated body guard
    if merchant_message.strip() == state.last_send_body.strip() and merchant_message:
        # Same message as what Vera sent — possible echo
        state.status = "ended"
        return {"action": "end", "rationale": "Echo detected; ending to avoid loop."}

    # Default: continue conversation (handed off to LLM in bot.py)
    return {"action": "continue", "rationale": "Continue to LLM composer."}
