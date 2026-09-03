"""Thin LLM wrapper for Mandate Rescue (additive, narration-only).

This module adds *natural-language generation* on top of the existing deterministic
engine. It NEVER makes decisions: scoring, the retry cap, compliance status, and the
dunning stage are all computed by the rule-based code and passed in here as ground
truth. The LLM only turns those real facts into readable prose.

Design goals:
- Groq (OpenAI-compatible) chat-completions endpoint, key from env GROQ_API_KEY.
- Zero extra pip dependencies: uses urllib so the demo runs on a stock Flask install.
- Graceful degradation: any error/timeout falls back to the existing template text,
  so a network hiccup never breaks the demo.
- Response cache keyed by (case_id, type) so re-running the agent during testing does
  not burn API calls on cases we've already generated for.

If GROQ_API_KEY is unset, every call transparently returns the template fallback.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

import messaging

# Server-side logger. The LLM sits behind graceful template fallbacks, so callers
# never see a stack trace -- but we MUST record the real underlying error here so
# failures (rate limits, timeouts, bad responses) are diagnosable from the logs
# instead of vanishing silently.
log = logging.getLogger("mandate_rescue.llm")

# --- Configuration ----------------------------------------------------------

# OpenAI-compatible endpoint. Groq's default is the one below; override via env t
# point at any OpenAI-compatible server (e.g. OpenAI itself) without code changes.
API_BASE = os.environ.get("LLM_API_BASE", "https://api.groq.com/openai/v1")
API_URL = API_BASE.rstrip("/") + "/chat/completions"

# A fast, inexpensive instruct model is plenty for one-paragraph narration.
# openai/gpt-oss-20b is the standard fast model available on GroqCloud.
# Override via LLM_MODEL env var to point at any OpenAI-compatible model
# (e.g. LLM_MODEL=gpt-4o-mini for OpenAI, LLM_MODEL=llama-3.3-70b-versatile
# for accounts with Llama access).
MODEL = os.environ.get("LLM_MODEL", "llama3-8b-8192")

# Short timeout: this is decoration on top of a working system, so we would rather
# fall back to templates quickly than make the UI wait on a slow call.
REQUEST_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "5"))

# Retry policy for TRANSIENT failures (HTTP 429 rate-limit, HTTP 5xx, timeouts,
# transient network errors). A malformed/misshapen response or an auth error (401/403)
# is NOT transient, so we do not retry those.
# Rate-limit (429): only 1 retry — the TPM window is 1 min, so retrying immediately
# just burns more time. Fail fast and use the template fallback instead.
# Other transient errors (5xx, timeout): up to LLM_MAX_ATTEMPTS retries.
LLM_MAX_ATTEMPTS = int(os.environ.get("LLM_MAX_ATTEMPTS", "2"))
LLM_MAX_ATTEMPTS_RATE_LIMIT = int(os.environ.get("LLM_MAX_ATTEMPTS_RATE_LIMIT", "1"))
LLM_BACKOFF_BASE = float(os.environ.get("LLM_BACKOFF_BASE", "0.5"))  # seconds

# Failure-reason codes surfaced to callers via the last-error channel so the API can
# tailor the user-facing message (rate-limited vs. temporarily down vs. not understood).
ERR_RATE_LIMIT = "rate_limit"
ERR_TIMEOUT = "timeout"
ERR_NETWORK = "network"
ERR_HTTP = "http_error"
ERR_BAD_RESPONSE = "bad_response"
ERR_NO_KEY = "no_key"

# Reasons we treat as transient and therefore worth retrying.
_TRANSIENT = frozenset({ERR_RATE_LIMIT, ERR_TIMEOUT, ERR_NETWORK})

# The most recent low-level failure reason (one of the ERR_* codes), or None on
# success. Set by _chat(); read by translate_query() so /api/ask can distinguish a
# transient outage from a genuinely unclear question. Not thread-safe by design --
# this is a single-worker demo; a multi-worker deploy would thread this through
# return values instead.
_LAST_ERROR = None


def last_error():
    """Return the most recent _chat failure code (ERR_*), or None after a success."""
    return _LAST_ERROR

# API key is read from the environment only. Never hardcode it.
def _api_key():
    return os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY")


# --- Response cache ---------------------------------------------------------
# Keyed by "<case_id>:<type>" so repeated agent runs / drawer opens reuse output.
_CACHE = {}


def clear_cache():
    """Drop all cached generations (handy between full agent runs in tests)."""
    _CACHE.clear()



_LIVE_BUDGET = None     # None = no restriction; set() = suppress all
_SUPPRESS_LLM = False   # set by set_live_budget([], suppress=True)


def set_live_budget(case_ids, suppress=False):
    """Restrict live LLM generation.

    - case_ids=None, suppress=False: no restriction — every case may use the LLM
      (default, used by drawer/ask endpoints).
    - case_ids=<iterable>, suppress=False: only these case_ids may use the LLM.
    - suppress=True (regardless of case_ids): suppress ALL LLM calls — every call
      falls back to templates. Used by benchmark / Monte Carlo runs.
    """
    global _LIVE_BUDGET, _SUPPRESS_LLM
    _SUPPRESS_LLM = suppress
    _LIVE_BUDGET = None if case_ids is None else set(str(c) for c in case_ids)


def _llm_allowed(case_id):
    """True if this case may hit the live LLM (always true when no budget is set)."""
    if _SUPPRESS_LLM:
        return False
    return (_LIVE_BUDGET is None) or (str(case_id) in _LIVE_BUDGET)


def _cache_key(case_id, kind, *extra):
    parts = [str(case_id), str(kind)] + [str(e) for e in extra]
    return ":".join(parts)


# --- Low-level call ----------------------------------------------------------

def _classify_http_error(err):
    """Map an HTTPError to an ERR_* code (429 -> rate_limit, 5xx -> transient http)."""
    code = getattr(err, "code", None)
    if code == 429:
        return ERR_RATE_LIMIT
    if code is not None and 500 <= code < 600:
        # Server-side hiccup: transient, retry it (reuse the network bucket).
        return ERR_NETWORK
    # 4xx (auth, bad request, etc.) is a real, non-transient error.
    return ERR_HTTP


def _attempt_once(req):
    """Make one HTTP call. Returns (text, None) on success or (None, err_code) on failure.

    Never raises: every failure is classified into an ERR_* code so the retry loop
    can decide whether to back off and try again, and so the real cause is logged.
    """
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"].strip()
        if not text:
            return None, ERR_BAD_RESPONSE
        return text, None
    except urllib.error.HTTPError as e:
        code = _classify_http_error(e)
        # Log the status and (truncated) body so a 429/4xx/5xx is diagnosable.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        log.warning("LLM HTTP %s (%s): %s", getattr(e, "code", "?"), code, detail)
        return None, code
    except (TimeoutError, urllib.error.URLError) as e:
        # URLError commonly wraps a socket timeout; treat those as timeouts.
        reason = getattr(e, "reason", e)
        is_timeout = isinstance(e, TimeoutError) or "timed out" in str(reason).lower()
        code = ERR_TIMEOUT if is_timeout else ERR_NETWORK
        log.warning("LLM %s: %s", code, reason)
        return None, code
    except (KeyError, IndexError, ValueError) as e:
        # Response parsed but was not the expected shape (or was not valid JSON).
        log.warning("LLM bad response shape: %s", e)
        return None, ERR_BAD_RESPONSE
    except OSError as e:
        log.warning("LLM network/OS error: %s", e)
        return None, ERR_NETWORK


def _chat(system_prompt, user_prompt, max_tokens=320, temperature=0.4):
    """Call the chat-completions endpoint, with retry on transient failures.

    Returns the text on success, or None on failure. On failure, the specific reason
    is recorded in module-level `_LAST_ERROR` (readable via last_error()) and logged
    server-side, so callers can degrade gracefully while the real cause is preserved.

    Transient failures (HTTP 429, HTTP 5xx, timeouts, network errors) are retried up
    to LLM_MAX_ATTEMPTS times with exponential backoff. Non-transient failures (auth
    errors, malformed responses) fail fast with no retry.
    """
    global _LAST_ERROR
    key = _api_key()
    if not key:
        _LAST_ERROR = ERR_NO_KEY
        return None

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Groq's Cloudflare edge rejects the default urllib User-Agent with a
            # 403 (error 1010), so send an explicit one.
            "User-Agent": "MandateRescue/1.0",
        },
        method="POST",
    )

    last_code = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        text, code = _attempt_once(req)
        if text is not None:
            _LAST_ERROR = None
            return text
        last_code = code
        # Rate-limit: use the lower retry cap to fail fast and save latency.
        # Other transient errors: use the full retry budget.
        max_for_this_error = (
            LLM_MAX_ATTEMPTS_RATE_LIMIT if code == ERR_RATE_LIMIT else LLM_MAX_ATTEMPTS
        )
        if code in _TRANSIENT and attempt < max_for_this_error:
            backoff = LLM_BACKOFF_BASE * (2 ** (attempt - 1))
            log.info("LLM transient failure (%s), retry %d/%d after %.2fs",
                     code, attempt, max_for_this_error - 1, backoff)
            time.sleep(backoff)
            continue
        break

    _LAST_ERROR = last_code
    log.warning("LLM call failed after %d attempt(s); final reason=%s",
                attempt, last_code)
    return None


# --- Public API --------------------------------------------------------------

_REASONING_SYSTEM = (
    "You are a payment-recovery operations analyst. You are given the FACTS of an "
    "already-made, rule-based decision about a failed auto-debit mandate. Your job is "
    "ONLY to explain that decision in one natural, specific paragraph. You must not "
    "invent any numbers, amounts, dates, percentages, or facts that are not in the "
    "provided data. Do not change or second-guess the decision. Do not add a preamble "
    "or a sign-off. Write 2-4 sentences, plain and concrete."
)


def generate_reasoning(case_data, decision_factors):
    """Narrate an already-made rule-based decision in one grounded paragraph.

    Args:
        case_data: the mandate_failures dict (real amount, reason, tenure, etc.).
        decision_factors: dict describing what the deterministic engine decided,
            including a 'ground_truth' string that is the authoritative rule-based
            reasoning. The LLM elaborates on this; it never decides anything.

    Returns the LLM paragraph, or the ground-truth string on any failure.
    """
    ground_truth = str(decision_factors.get("ground_truth", "")).strip()
    case_id = case_data.get("customer_id", "unknown")
    kind = "reasoning:" + str(decision_factors.get("event_type", "decision"))

    ck = _cache_key(case_id, kind)
    if ck in _CACHE:
        return _CACHE[ck]

    # Only pass real, structured facts. This is the LLM's entire universe of truth.
    facts = {
        "amount_rupees": int(round(float(case_data.get("amount", 0)))),
        "failure_reason": case_data.get("failure_reason"),
        "merchant_category": case_data.get("merchant_category"),
        "customer_tenure_months": case_data.get("customer_tenure_months"),
        "past_payment_success_rate": case_data.get("past_payment_success_rate"),
        "past_retry_count": case_data.get("past_retry_count"),
        "failure_date": case_data.get("failure_date"),
        "recoverability_score": decision_factors.get("score"),
        "strategy_chosen": decision_factors.get("strategy"),
        "score_breakdown": decision_factors.get("score_factors"),
    }
    user_prompt = (
        "FACTS (the only truth you may use):\n"
        + json.dumps(facts, indent=2, default=str)
        + "\n\nRULE-BASED DECISION ALREADY MADE (ground truth to explain):\n"
        + ground_truth
        + "\n\nWrite one grounded paragraph (2-4 sentences) explaining why this "
        "decision was made, using only the facts above. Refer to the real numbers "
        "exactly as given. Do not invent anything."
    )

    text = _chat(_REASONING_SYSTEM, user_prompt, max_tokens=512, temperature=0.4) \
        if _llm_allowed(case_id) else None
    result = text if text else ground_truth
    _CACHE[ck] = result
    return result


_MESSAGE_SYSTEM = (
    "You write short customer nudge messages for failed auto-pay (UPI mandate) "
    "collections in India. Keep it to 2-3 sentences, warm and clear, never "
    "threatening. Use ONLY the amount and context provided; never invent amounts, "
    "dates, links, offers, or account details. Output only the message text with no "
    "quotes, labels, or preamble."
)


def generate_message(case_data, tone="Standard", channel="SMS"):
    """Generate one nudge message for a case in the given tone and channel.

    Tones mirror the existing template options exactly: 'Standard' (English) and
    'Hinglish'. On any failure, falls back to the corresponding template string from
    messaging.py, so output shape and language options are unchanged.
    """
    tone_norm = "Hinglish" if str(tone).strip().lower() == "hinglish" else "Standard"
    case_id = case_data.get("customer_id", "unknown")
    ck = _cache_key(case_id, "message", tone_norm, channel)
    if ck in _CACHE:
        return _CACHE[ck]

    # Deterministic template is both the fallback and a style/grounding reference.
    templates = messaging.generate_messages(case_data, channel=channel)
    fallback = templates["hinglish"] if tone_norm == "Hinglish" else templates["standard"]

    amt = int(round(float(case_data.get("amount", 0))))
    cat = messaging.CATEGORY_LABEL.get(case_data.get("merchant_category", ""), "payment")
    facts = {
        "amount_rupees": amt,
        "failure_reason": case_data.get("failure_reason"),
        "category": cat,
        "channel": channel if channel in messaging.CHANNELS else "SMS",
    }
    lang = ("Hinglish (Romanized Hindi + English mix, friendly)"
            if tone_norm == "Hinglish" else "clear Standard English")
    user_prompt = (
        "FACTS (only truth you may use):\n"
        + json.dumps(facts, indent=2, default=str)
        + f"\n\nWrite the nudge message in {lang}. Mention the exact amount "
        f"Rs {amt} and the reason for failure. Do not invent links, dates, or "
        "amounts. Reference style example: " + fallback
    )

    text = _chat(_MESSAGE_SYSTEM, user_prompt, max_tokens=512, temperature=0.6) \
        if _llm_allowed(case_id) else None
    result = text if text else fallback
    _CACHE[ck] = result
    return result


def generate_message_variants(case_data, channel="SMS"):
    """Return the same dict shape as messaging.generate_messages, but LLM-authored.

    Keeps 'standard' and 'hinglish' keys plus the channel metadata so existing
    callers (agent dunning, /api/messages, audit drawer) work unchanged. Any
    variant that fails to generate simply carries its template fallback.
    """
    base = messaging.generate_messages(case_data, channel=channel)
    base["standard"] = generate_message(case_data, tone="Standard", channel=channel)
    base["hinglish"] = generate_message(case_data, tone="Hinglish", channel=channel)
    return base


# --- Natural-language query translation -------------------------------------
# The LLM's ONLY job here is to translate a user's question into a small, closed
# set of filter parameters. It never returns case data. Code then executes those
# parameters as real parameterized SQL against the database (see query.py). This is
# the same trust boundary as the reasoning feature: LLM interprets intent, code runs
# on real data.

# Allowed filter keys the LLM may emit. Anything else is dropped by query.py.
FILTER_SPEC_KEYS = (
    "failure_reason",       # insufficient_funds | mandate_expired | mandate_revoked | bank_technical_error
    "merchant_category",    # subscription | emi | insurance | utility
    "compliance_status",    # RBI-compliant | non-compliant
    "case_status",          # new | in_progress | recovered | escalated | promised | broken_promise
    "health_band",          # healthy | at-risk | high-risk
    "amount_min", "amount_max",
    "score_min", "score_max",
    "over_limit",           # true = amount exceeds mandate_limit
    "dunning_stage_min",    # 1..3
    "sort_by_amount",       # "desc" | "asc"
)

_QUERY_SYSTEM = (
    "You translate an ops user's natural-language question about failed payment "
    "cases into a small JSON filter object. You NEVER answer with case data or "
    "counts. You ONLY output a JSON object using these keys (omit any that do not "
    "apply): "
    + ", ".join(FILTER_SPEC_KEYS)
    + ". Allowed values: failure_reason in [insufficient_funds, mandate_expired, "
    "mandate_revoked, bank_technical_error]; merchant_category in [subscription, "
    "emi, insurance, utility]; compliance_status in [RBI-compliant, non-compliant]; "
    "case_status in [new, in_progress, recovered, escalated, promised, "
    "broken_promise]; health_band in [healthy, at-risk, high-risk]; over_limit is a "
    "boolean; amounts are integer rupees; score is 0-100; dunning_stage_min is 1-3; "
    "sort_by_amount is 'desc' or 'asc'. 'high-value' or 'high value' means "
    "amount_min around 5000. 'high churn risk' or 'at risk of churn' means "
    "health_band 'high-risk'. Output ONLY the JSON object, no prose, no code fence."
)


def translate_query(question):
    """Translate an NL question into a filter-spec dict. Returns {} if unclear.

    Returns a dict of allowed keys only. On any LLM/parse failure returns None so the
    caller can distinguish 'LLM unavailable' from 'understood but no filters'.
    """
    q = (question or "").strip()
    if not q:
        return {}
    ck = _cache_key("nlq", "filter", q.lower())
    if ck in _CACHE:
        return _CACHE[ck]

    # Budget must be generous enough that the JSON object is never truncated
    # mid-value (a cut-off like {"amount_min": would fail to parse -> {}).
    text = _chat(_QUERY_SYSTEM, "Question: " + q, max_tokens=512, temperature=0.0)
    if not text:
        return None

    # The model may wrap JSON in a fence or add stray text; extract the object.
    raw = text.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        _CACHE[ck] = {}
        return {}
    try:
        parsed = json.loads(raw[start:end + 1])
    except ValueError:
        _CACHE[ck] = {}
        return {}
    if not isinstance(parsed, dict):
        _CACHE[ck] = {}
        return {}

    # Keep only allowed keys; drop null/empty values.
    spec = {}
    for k in FILTER_SPEC_KEYS:
        if k in parsed and parsed[k] not in (None, "", []):
            spec[k] = parsed[k]
    _CACHE[ck] = spec
    return spec


def summarize_results(question, count, sample):
    """One-line natural-language summary of what a query found (grounded in count).

    `count` is the real number of matching rows; `sample` is a short list of matching
    cases (already fetched from the DB). Falls back to a plain templated sentence.
    """
    fallback = (f"Found {count} matching case" + ("" if count == 1 else "s") + ".")
    if count == 0:
        return fallback
    sample_facts = [
        {"customer_id": s.get("customer_id"),
         "amount_rupees": int(round(float(s.get("amount", 0)))),
         "failure_reason": s.get("failure_reason"),
         "case_status": s.get("case_status")}
        for s in (sample or [])[:5]
    ]
    system = (
        "You write a single factual sentence summarizing a query result set for an "
        "ops dashboard. Use ONLY the count and sample provided. Never invent numbers "
        "or totals beyond the given count. One sentence, no preamble."
    )
    user = (
        f"User question: {question}\n"
        f"Exact number of matching cases: {count}\n"
        f"Sample of matches (not the full set): {json.dumps(sample_facts, default=str)}\n"
        "Write one sentence stating what was found, leading with the exact count."
    )
    text = _chat(system, user, max_tokens=256, temperature=0.3)
    return text if text else fallback
