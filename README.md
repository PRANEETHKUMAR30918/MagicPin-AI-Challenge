# Vera Decision Engine — Magicpin AI Challenge Submission

## Approach

This bot implements a **Claude-powered, 4-context composition pipeline** deployed as a Flask HTTP server.

### Architecture

```
Judge Harness
     │
     ▼
/v1/context  →  Thread-safe ConcurrentStore (scope, context_id) → {version, payload}
/v1/tick     →  TriggerDispatcher → ComposeMessage() → Claude claude-sonnet-4-20250514 (temp=0)
/v1/reply    →  ConversationManager → Auto-reply/opt-out guard → Claude reply composer
/v1/healthz  →  Liveness + context_loaded counts
/v1/metadata →  Bot identity
```

### Composition Pipeline

1. **Context resolution**: For each active trigger, load merchant + category + optional customer context.
2. **Suppression pre-check**: Skip if suppression_key already sent, merchant suppressed, or conversation ended.
3. **Full 4-context prompt**: Pack all relevant data (perf numbers, digest items, peer stats, signals) into a structured prompt.
4. **Claude claude-sonnet-4-20250514 (temperature=0)**: Deterministic composition anchored on real numbers.
5. **JSON validation**: Parse and validate all required fields before returning.
6. **Suppression post-action**: Mark key as used; suppress merchant on opt-out.

### Key Design Decisions

**Specificity over generics**: Every prompt explicitly instructs the model to anchor on verifiable data (views, CTR, digest citations, peer stats). Generic "% off" language is banned in the prompt.

**Auto-reply detection**: Regex-based detection before any LLM call. Escalates: acknowledge → wait 24h → end (3-strike).

**Intent-transition routing**: Regex detects commit signals ("yes", "ok let's do it", "go ahead"). Prompt includes an IMPORTANT directive to switch modes immediately.

**Thread safety**: All shared state uses `threading.RLock()`. Concurrent reads/writes are safe under Flask's `threaded=True` + gunicorn `gthread` workers.

**Graceful degradation**: If Claude API is unavailable, a deterministic fallback fires. System never crashes on malformed input.

**Restraint**: If the model returns `{"send": false}` or composition fails, we return `{"actions": []}` from tick — no spam.

### Tradeoffs

- **In-memory only**: Context is lost on restart, but this matches the test window design.
- **Sequential within tick**: For simplicity, triggers processed sequentially (each completes before next). At 10 req/s from judge this is fine; could parallelize with thread pool if needed.
- **Single LLM call per message**: No retrieval layer. Given the contexts are compact and passed directly, this is sufficient for < 30s latency.

### What would have helped most

- Merchant's **actual conversation history** with Vera (prior sessions) — would allow anti-repetition at the session level.
- **Real-time search counts** per merchant locality — the "6,777 missed searches" hook (Pattern C) is the most compelling lever but requires live data.

## Setup

```bash
pip install flask requests gunicorn
export ANTHROPIC_API_KEY=sk-ant-...
python bot.py            # dev mode on port 8080
# OR
gunicorn bot:app --workers 4 --threads 2 --worker-class gthread --bind 0.0.0.0:8080 --timeout 30
```

## Deploy on Render

1. Push this directory to GitHub.
2. Create a new Render Web Service, connect your repo.
3. **Build command**: `pip install -r requirements.txt`
4. **Start command**: `gunicorn bot:app --workers 4 --threads 2 --worker-class gthread --bind 0.0.0.0:$PORT --timeout 30`
5. Set env var `ANTHROPIC_API_KEY` in Render dashboard.
6. Render auto-assigns a public HTTPS URL.

## Test locally

```bash
export BOT_URL=http://localhost:8080

# Health check
curl $BOT_URL/v1/healthz

# Push category context
curl -X POST -H "Content-Type: application/json" \
  -d @dataset/categories/dentists.json \  # wrap in {scope,context_id,version,payload}
  $BOT_URL/v1/context

# Tick
curl -X POST -H "Content-Type: application/json" \
  -d '{"now":"2026-04-26T10:35:00Z","available_triggers":["trg_001_research_digest_dentists"]}' \
  $BOT_URL/v1/tick

# Reply
curl -X POST -H "Content-Type: application/json" \
  -d '{"conversation_id":"conv_001","merchant_id":"m_001_drmeera_dentist_delhi","from_role":"merchant","message":"Yes please send it","received_at":"2026-04-26T10:42:00Z","turn_number":2}' \
  $BOT_URL/v1/reply
```

## Run the judge simulator

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```
