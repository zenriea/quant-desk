"""AI analyst client with budget enforcement and a full audit trail.

Providers (QD_LLM_PROVIDER, inferred if unset):
  * ``openai_compatible`` - any OpenAI-style /chat/completions API: OpenAI, Google Gemini
    (OpenAI-compatible endpoint), Groq, OpenRouter, Mistral, DeepSeek, Together, or a local
    Ollama server. Plain HTTPS; set QD_LLM_BASE_URL, QD_LLM_API_KEY, QD_LLM_MODEL.
  * ``anthropic`` - Claude via the official Anthropic SDK.

Every HTTP attempt is stored in ``llm_calls`` (prompt, response, tokens, cost). A call is
refused *before* it is made if its worst-case cost would exceed the daily or monthly budget.
Responses must be JSON matching the task's schema; the schema is enforced by the provider
when it supports it, and always validated here (with one repair round-trip on mismatch).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import time

from .. import clock, db, settings
from ..config import FALLBACK_PRICING, MODEL_PRICING, load_config

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


# ------------------------------------------------------------------ pricing & budget
FREE_MODELS: set[str] = set()  # free-tier helper models: cost $0 and never touch the budget


def pricing(model: str):
    if model in FREE_MODELS:
        return (0.0, 0.0, 0.0, 0.0)
    cfg = load_config()
    if cfg.llm_provider == "openai_compatible":
        return (cfg.llm_price_in, cfg.llm_price_out, cfg.llm_price_in, cfg.llm_price_in)
    return MODEL_PRICING.get(model, FALLBACK_PRICING)


def cost_of(model: str, usage: dict) -> float:
    pi, po, pw, pr = pricing(model)
    return (usage.get("input_tokens", 0) * pi + usage.get("output_tokens", 0) * po
            + usage.get("cache_write_tokens", 0) * pw + usage.get("cache_read_tokens", 0) * pr) / 1e6


def spent_since(epoch: float) -> float:
    r = db.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_calls WHERE ts >= ?", (epoch,))
    return float(r["c"])


def budget_status() -> dict:
    now = clock.now()
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    month0 = dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp()
    day0 = now - now % 86400
    b = settings.get("budget")
    m, dd = spent_since(month0), spent_since(day0)
    return {"month_spent": round(m, 4), "day_spent": round(dd, 4), "monthly_cap": b["monthly_usd"],
            "daily_cap": b["daily_usd"], "month_remaining": round(max(0, b["monthly_usd"] - m), 4),
            "day_remaining": round(max(0, b["daily_usd"] - dd), 4),
            "calls_today": db.q1("SELECT COUNT(*) n FROM llm_calls WHERE ts>=? AND status='ok'", (day0,))["n"]}


# ------------------------------------------------------------------ schema validation
def validate(obj, schema: dict, path: str = "$") -> list[str]:
    """Minimal JSON-schema check for the subset our schemas use. Returns error strings."""
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if not validate(obj, sub, path):
                return []
        return [f"{path}: does not match any allowed type"]
    t = schema.get("type")
    errs: list[str] = []
    if t == "object":
        if not isinstance(obj, dict):
            return [f"{path}: expected object"]
        for k in schema.get("required", []):
            if k not in obj:
                errs.append(f"{path}.{k}: missing")
        for k, sub in (schema.get("properties") or {}).items():
            if k in obj:
                errs += validate(obj[k], sub, f"{path}.{k}")
        if schema.get("additionalProperties") is False:
            for k in obj.keys() - (schema.get("properties") or {}).keys():
                errs.append(f"{path}.{k}: unexpected property")
    elif t == "array":
        if not isinstance(obj, list):
            return [f"{path}: expected array"]
        for i, item in enumerate(obj):
            errs += validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if not isinstance(obj, str):
            errs.append(f"{path}: expected string")
    elif t == "number":
        if isinstance(obj, bool) or not isinstance(obj, (int, float)) or not math.isfinite(obj):
            errs.append(f"{path}: expected finite number")
    elif t == "integer":
        if isinstance(obj, bool) or not (isinstance(obj, int) or (isinstance(obj, float) and obj.is_integer())):
            errs.append(f"{path}: expected integer")
    elif t == "boolean":
        if not isinstance(obj, bool):
            errs.append(f"{path}: expected boolean")
    elif t == "null":
        if obj is not None:
            errs.append(f"{path}: expected null")
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    return errs


def _coerce_ints(obj, schema):
    """Providers sometimes return 20.0 for integers; normalise so downstream code is exact."""
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if not validate(obj, sub):
                return _coerce_ints(obj, sub)
        return obj
    t = schema.get("type")
    if t == "integer" and isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if t == "object" and isinstance(obj, dict):
        props = schema.get("properties") or {}
        return {k: _coerce_ints(v, props[k]) if k in props else v for k, v in obj.items()}
    if t == "array" and isinstance(obj, list):
        return [_coerce_ints(v, schema.get("items", {})) for v in obj]
    return obj


def extract_json(text: str):
    """Parse a JSON object from model output, tolerating <think> blocks, ```json fences or prose."""
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


# ------------------------------------------------------------------ client
_compat_quirks: dict = {}  # learned per base_url: which request options the provider rejects


# Free helpers that fail in a way that will repeat on the next call are benched instead of being
# called (and retried) every cycle: exhausted daily quota, a model the provider no longer serves
# for free, or a prompt above the provider's tokens-per-minute cap.
HELPER_BENCH_SEC = 6 * 3600
_benched_until: dict[tuple, float] = {}   # (base_url, model) -> epoch
_too_large: dict[tuple, int] = {}         # (base_url, model) -> smallest prompt size (chars) that was refused


def _daily_quota(text: str) -> bool:
    t = (text or "").lower()
    return "quota" in t and ("per day" in t or "perday" in t or "daily" in t)


class LLM:
    _sleep = staticmethod(time.sleep)

    def __init__(self, client=None, http_post=None, route: dict | None = None):
        """``client``: injected Anthropic client (tests). ``http_post``: injected
        requests.post-compatible callable for the OpenAI-compatible path (tests).
        ``route``: a free helper provider {name, base_url, api_key, model}."""
        self.cfg = load_config()
        self._client = client
        self._post = http_post
        self.route = route
        if route is not None:
            self.provider = "openai_compatible"
            FREE_MODELS.add(route["model"])
        elif client is not None:
            self.provider = "anthropic"
        elif http_post is not None:
            self.provider = "openai_compatible"
        else:
            self.provider = self.cfg.llm_provider

    @property
    def available(self) -> bool:
        return self.route is not None or self._client is not None or self._post is not None or not self.cfg.demo_llm

    @property
    def _key(self) -> tuple:
        return (self.route["base_url"] if self.route else self.cfg.llm_base_url, self.model)

    @property
    def benched(self) -> bool:
        return self.route is not None and _benched_until.get(self._key, 0) > clock.now()

    def _bench(self, status: int, daily_quota: bool) -> None:
        if self.route is not None:
            now = clock.now()
            until = now + HELPER_BENCH_SEC
            if daily_quota:
                # shortcut: daily free quotas reset at midnight Pacific; 08:00 UTC is that in winter, an hour late in summer
                until = max(until, (now - 8 * 3600) // 86400 * 86400 + 86400 + 8 * 3600)
            _benched_until[self._key] = until
            log.warning("benching helper %s for %.1fh after HTTP %s", self.model, (until - now) / 3600, status)

    @property
    def model(self) -> str:
        if self.route:
            return self.route["model"]
        return self.cfg.llm_model or ("claude-haiku-5-5" if self.provider == "anthropic" else "unset")

    # -- shared bookkeeping
    def _precheck(self, purpose: str, system: str, user: str, max_tokens: int) -> None:
        pi, po, _, _ = pricing(self.model)
        worst = ((len(system) + len(user)) / 3.0 * pi + max_tokens * po) / 1e6
        b = budget_status()
        if worst > b["day_remaining"] or worst > b["month_remaining"]:
            raise BudgetExceeded(f"{purpose}: worst-case cost ${worst:.3f} exceeds remaining budget "
                                 f"(day ${b['day_remaining']:.2f}, month ${b['month_remaining']:.2f})")

    def _record_start(self, purpose: str, request: dict) -> int:
        return db.ex("INSERT INTO llm_calls(ts, purpose, model, status, request) VALUES(?,?,?,?,?)",
                     (clock.now(), purpose, self.model, "pending", json.dumps(request)))

    def _record_end(self, cid, status, err, served, usage, text) -> None:
        cost = 0.0 if self.route is not None else cost_of(served or self.model, usage)
        db.ex("UPDATE llm_calls SET status=?, error=?, served_model=?, input_tokens=?, output_tokens=?, "
              "cache_write_tokens=?, cache_read_tokens=?, cost_usd=?, response=? WHERE id=?",
              (status, err, served, usage["input_tokens"], usage["output_tokens"], usage.get("cache_write_tokens", 0),
               usage.get("cache_read_tokens", 0), cost, (text or "")[:200000], cid))

    def call(self, purpose: str, system: str, user: str, schema: dict, max_tokens: int = 16000) -> tuple[dict, int]:
        if self.provider == "openai_compatible":
            return self._call_compat(purpose, system, user, schema, max_tokens)
        if self.provider == "anthropic":
            return self._call_anthropic(purpose, system, user, schema, max_tokens)
        raise LLMError("no AI provider configured")

    # -- OpenAI-compatible chat completions
    def _call_compat(self, purpose, system, user, schema, max_tokens):
        import requests
        post = self._post or requests.post
        base = self.route["base_url"] if self.route else self.cfg.llm_base_url
        api_key = self.route["api_key"] if self.route else self.cfg.llm_api_key
        quirks = _compat_quirks.setdefault((base, self.model), {"format": "json_schema", "tok": "max_tokens"})
        instruction = ("\n\nRespond with ONE JSON object and nothing else. It must match this JSON Schema "
                       "(all listed fields required; use null where allowed):\n" + json.dumps(schema))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user + instruction}]
        size = len(system) + len(messages[-1]["content"])
        if self.benched:
            raise LLMError(f"{purpose}: {self.model} is benched after a persistent failure")
        if self.route is not None and size >= _too_large.get(self._key, size + 1):
            raise LLMError(f"{purpose}: prompt too large for {self.model}'s rate limit")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        repaired = False
        transient = 0
        for _attempt in range(8):
            self._precheck(purpose, system, json.dumps(messages), max_tokens)
            body = {"model": self.model, "messages": messages, quirks["tok"]: max_tokens}
            if quirks["format"] == "json_schema":
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": purpose.replace("-", "_")[:60], "schema": schema, "strict": True}}
            elif quirks["format"] == "json_object":
                body["response_format"] = {"type": "json_object"}
            cid = self._record_start(purpose, {"provider": base, "model": self.model, "system": system,
                                               "user": messages[-1]["content"], "schema": schema,
                                               "max_tokens": max_tokens, "format": quirks["format"]})
            t0 = time.time()
            try:
                r = post(f"{base}/chat/completions", headers=headers, json=body, timeout=300)
            except Exception as e:
                error = f"Provider request failed ({type(e).__name__})"
                self._record_end(cid, "error", error, None, {"input_tokens": 0, "output_tokens": 0}, "")
                raise LLMError(f"{purpose}: {error}") from None
            daily_quota = r.status_code == 429 and _daily_quota(r.text)
            persistent = daily_quota or r.status_code in (404, 413)
            if r.status_code in (429, 500, 502, 503, 504) and transient < 2 and not persistent:
                # overloaded / rate-limited / gateway timeout: brief backoff, then retry
                transient += 1
                self._record_end(cid, "retry", f"HTTP {r.status_code}, retrying", None,
                                 {"input_tokens": 0, "output_tokens": 0}, "")
                self._sleep(4 * transient)
                continue
            if r.status_code in (400, 422) and self._adapt(quirks, r.text):
                self._record_end(cid, "retry", f"HTTP {r.status_code}, adapting request", None,
                                 {"input_tokens": 0, "output_tokens": 0}, "")
                continue
            if r.status_code >= 400:
                error = f"Provider returned HTTP {r.status_code}"
                self._record_end(cid, "error", error, None, {"input_tokens": 0, "output_tokens": 0}, "")
                if r.status_code == 413 and self.route is not None:
                    _too_large[self._key] = min(_too_large.get(self._key, size), size)
                elif persistent:
                    self._bench(r.status_code, daily_quota)
                raise LLMError(f"{purpose}: {error}")
            try:
                data = r.json()
            except ValueError:
                error = "Provider returned an invalid JSON response"
                self._record_end(cid, "error", error, None, {"input_tokens": 0, "output_tokens": 0}, "")
                raise LLMError(f"{purpose}: {error}") from None
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            u = data.get("usage") or {}
            usage = {"input_tokens": int(u.get("prompt_tokens") or (len(system) + len(messages[-1]["content"])) / 3),
                     "output_tokens": int(u.get("completion_tokens") or len(text) / 3)}
            served = data.get("model") or self.model
            finish = choice.get("finish_reason")
            if msg.get("refusal") or finish == "content_filter":
                self._record_end(cid, "refused", str(msg.get("refusal") or finish)[:500], served, usage, text)
                raise LLMError(f"{purpose}: model declined")
            if finish == "length":
                self._record_end(cid, "truncated", "hit max tokens", served, usage, text)
                raise LLMError(f"{purpose}: response hit max_tokens before completing")
            try:
                parsed = extract_json(text)
                errs = validate(parsed, schema)
            except ValueError as e:
                parsed, errs = None, [f"not valid JSON: {e}"]
            if not errs:
                self._record_end(cid, "ok", None, served, usage, text)
                log.info("llm %s ok %.1fs", purpose, time.time() - t0)
                return _coerce_ints(parsed, schema), cid
            self._record_end(cid, "invalid_json", "; ".join(errs)[:1000], served, usage, text)
            if repaired:
                raise LLMError(f"{purpose}: response did not match the schema: {'; '.join(errs[:5])}")
            repaired = True
            messages = messages + [{"role": "assistant", "content": text},
                                   {"role": "user", "content": "That response did not match the schema: "
                                    + "; ".join(errs[:10]) + ". Reply with the corrected JSON object only."}]
        raise LLMError(f"{purpose}: provider kept rejecting the request format")

    @staticmethod
    def _adapt(quirks: dict, err: str) -> bool:
        """Learn from a 400 which optional request feature the provider does not support."""
        e = err.lower()
        if "max_tokens" in e and quirks["tok"] == "max_tokens" and ("max_completion_tokens" in e or "unsupported" in e):
            quirks["tok"] = "max_completion_tokens"
            return True
        if ("response_format" in e or "json_schema" in e or "schema" in e) and quirks["format"] == "json_schema":
            quirks["format"] = "json_object"
            return True
        if ("response_format" in e or "json_object" in e) and quirks["format"] == "json_object":
            quirks["format"] = "none"
            return True
        return False

    # -- Anthropic (Claude)
    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key, max_retries=2, timeout=300.0)
        return self._client

    def _call_anthropic(self, purpose, system, user, schema, max_tokens):
        model = self.model
        self._precheck(purpose, system, user, max_tokens)
        cid = self._record_start(purpose, {"provider": "anthropic", "model": model, "system": system, "user": user,
                                           "schema": schema, "max_tokens": max_tokens, "effort": self.cfg.llm_effort})
        kwargs = dict(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.cfg.llm_effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            import anthropic
            try:
                if self.cfg.llm_fallbacks:
                    resp = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                            fallbacks="default", **kwargs)
                else:
                    resp = self.client.messages.create(**kwargs)
            except anthropic.BadRequestError as e:
                if self.cfg.llm_fallbacks and "fallback" in str(e).lower():
                    log.warning("Anthropic rejected fallbacks; retrying without them")
                    resp = self.client.messages.create(**kwargs)
                else:
                    raise
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            error = (f"Provider returned HTTP {status_code}" if isinstance(status_code, int)
                     else f"Provider request failed ({type(e).__name__})")
            db.ex("UPDATE llm_calls SET status='error', error=? WHERE id=?", (error, cid))
            raise LLMError(f"{purpose}: {error}") from None
        u = getattr(resp, "usage", None)
        usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0,
                 "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
        served = getattr(resp, "model", model) or model
        stop = getattr(resp, "stop_reason", None)
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        status, err, parsed = "ok", None, None
        if stop == "refusal":
            status, err = "refused", f"model declined: {getattr(getattr(resp, 'stop_details', None), 'category', None)}"
        elif stop == "max_tokens":
            status, err = "truncated", "response hit max_tokens before completing"
        else:
            try:
                parsed = json.loads(text)
                errs = validate(parsed, schema)
                if errs:
                    status, err, parsed = "invalid_json", "; ".join(errs)[:1000], None
            except ValueError as e:
                status, err = "invalid_json", str(e)
        self._record_end(cid, status, err, served, usage, text)
        if parsed is None:
            raise LLMError(f"{purpose}: {err}")
        return parsed, cid


# ------------------------------------------------------------------ multi-model routing
# Free-tier helpers switch on when their key is in the environment. Model env vars accept a
# single model or a comma-separated fallback list (because free model line-ups change often).
# Tried in this order. Gemini's free tier allows only ~20 requests a day, so it is the backup.
FREE_PRESETS = {
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "QD_OPENROUTER_MODEL", "nvidia/nemotron-3.5-super-vl:free"),
    "nvidia": ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "QD_NVIDIA_MODEL", "meta/llama-3.3-70b-instruct"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "QD_GEMINI_MODEL", "gemini-3.8-flash"),
}
# Frequent, long-prompt work goes to free models first; the paid model is the fallback.
FREE_FIRST = {"hypothesis", "hypothesis_repair", "review", "question"}


def _split_models(spec: str) -> list[str]:
    return list(dict.fromkeys(m.strip() for m in spec.split(",") if m.strip()))


def free_helpers() -> list[LLM]:
    from ..config import _env
    cfg = load_config()
    primary_base = cfg.llm_base_url if cfg.llm_provider == "openai_compatible" else None
    out = []
    for name, (key_env, base, model_env, default_model) in FREE_PRESETS.items():
        key = _env(key_env)
        if not key:
            continue
        models = _split_models(_env(model_env, default_model)) or _split_models(default_model)
        for model in models:
            if base == primary_base and model == cfg.llm_model:
                continue
            out.append(LLM(route={"name": name, "base_url": base, "api_key": key, "model": model}))
    return out


def llm_status(cfg=None) -> dict:
    """User-facing LLM mode summary, accounting for free-only setups with no paid primary key."""
    cfg = cfg or load_config()
    helpers = free_helpers()
    helper_labels = [f"{h.model} ({h.route['name']})" for h in helpers]
    if not cfg.demo_llm:
        return {"label": cfg.llm_label, "provider": cfg.llm_provider, "demo": False, "helpers": helper_labels}
    if helpers:
        first = helpers[0]
        providers = list(dict.fromkeys(h.route["name"] for h in helpers))
        if len(helpers) == 1:
            label = f"{first.model} ({first.route['name']}, free)"
        else:
            label = f"{first.model} (+{len(helpers) - 1} free: {', '.join(providers)})"
        return {"label": label, "provider": first.route["name"], "demo": False, "helpers": helper_labels}
    return {"label": "demo", "provider": cfg.llm_provider, "demo": True, "helpers": []}


class Router:
    """Same ``call`` interface as LLM, spread over the primary model and free helpers."""

    def __init__(self, primary: LLM, helpers: list[LLM]):
        self.primary = primary if primary.available else None
        self.helpers = helpers
        self.last: LLM | None = None
        self.notify = None  # optional progress(step, detail) callback, set per call by the caller

    @property
    def available(self) -> bool:
        return bool(self.primary or self.helpers)

    @property
    def model(self) -> str:
        return (self.primary or self.helpers[0]).model

    def _chain(self, purpose: str) -> list[LLM]:
        p = [self.primary] if self.primary else []
        return self.helpers + p if purpose in FREE_FIRST else p + self.helpers

    def call(self, purpose, system, user, schema, max_tokens=16000, chain=None):
        chain = self._chain(purpose) if chain is None else chain
        err: Exception = LLMError(f"{purpose}: no model available")
        for i, llm in enumerate(chain):
            if self.notify:
                self.notify("model", f"Asking {llm.model}" + (f" ({llm.route['name']})" if llm.route else ""))
            try:
                out = llm.call(purpose, system, user, schema, max_tokens)
                self.last = llm
                if self.notify:
                    self.notify("answered", f"{llm.model} answered")
                return out
            except (LLMError, BudgetExceeded) as e:
                log.warning("%s via %s failed (%s); trying next model", purpose, llm.model, e)
                if self.notify:
                    more = " - trying the next model" if i + 1 < len(chain) else ""
                    self.notify("model_failed", f"{llm.model} failed: {str(e)[:160]}{more}")
                err = e
        raise err

    def second_opinion(self, purpose, system, user, schema, max_tokens=16000):
        """A different model's answer, preferring an independent provider when available."""
        others = [h for h in self.helpers if h is not self.last]
        if not others:
            return None
        last_provider = self.last.route["name"] if self.last and self.last.route else None
        if last_provider:
            others = ([h for h in others if h.route["name"] != last_provider]
                      + [h for h in others if h.route["name"] == last_provider])
        try:
            return self.call(purpose, system, user, schema, max_tokens, chain=others)
        except (LLMError, BudgetExceeded):
            return None
