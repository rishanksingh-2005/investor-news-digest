"""Step 5, summaries: LLM / BART bullets, the number check, and LLM resilience."""

from __future__ import annotations

import os
import re
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
try:
    from openai import OpenAI            # also used for Ollama (OpenAI-compatible API)
except ImportError:
    OpenAI = None
try:
    import anthropic
except ImportError:
    anthropic = None

from . import extraction, health, models, patterns, settings, sources, state


# ============================================================================
# STEP 5 — Summarise to a bullet (Ollama, with offline fallback)
# ============================================================================
def _fallback_bullet(a: models.Article) -> str:
    content = a.body if (a.body and len(a.body) >= 120) else a.snippet
    if a.kind == "filing":
        form = re.sub(r"^SEC\s+|\s+filing\b.*$", "", a.headline).strip() or "Regulatory"
        desc = (a.snippet or "").strip()
        if desc and desc.upper() != form.upper():
            # EDGAR descriptions are often SHOUTED ("QUARTERLY REPORT"); item names aren't
            desc = desc.capitalize() if desc.isupper() else desc[0].upper() + desc[1:]
            return patterns._tidy_bullet(f"{form} filed with the SEC: {desc}")
        return f"{form} filing published on SEC EDGAR."
    content = (content or "").strip()
    if len(content) < 40:
        # A fragment (e.g. a bare source name in the feed description) makes a
        # worse bullet than the headline itself.
        return patterns._tidy_bullet(a.headline or content)
    sents = re.split(r"(?<=[.!?])\s+", content)
    lead = " ".join(sents[:2]).strip()
    return patterns._tidy_bullet(lead or a.headline)


# ----------------------------------------------------------------------------
# Number check: every figure in an AI-written bullet must come from the source.
# A small model can invent or garble a number ("£40m" -> "£400m", "18%" -> "81%"),
# which is the worst possible error in an investor brief. Bullets that fail are
# replaced by the article's own sentences, and never cached.
# ----------------------------------------------------------------------------
_SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mln": 1e6, "million": 1e6,
           "bn": 1e9, "b": 1e9, "billion": 1e9, "tn": 1e12, "trillion": 1e12}
_FIGURE_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"
    r"(?:\s?(k|thousand|mn|mln|m|million|bn|b|billion|tn|trillion)\b)?"
    r"(\s?(?:%|percent\b|per\s?cent\b|pct\b|percentage points?\b|pp\b))?", re.I)


def _figures(text: str) -> list[tuple]:
    """(value, decimals, scale, literal) for each number in the text."""
    out = []
    for m in _FIGURE_RE.finditer(text or ""):
        whole, frac, unit, pct = m.groups()
        num = float(whole.replace(",", "") + ("." + frac if frac else ""))
        scale = _SCALES.get((unit or "").lower(), 1.0)
        out.append((num * scale, len(frac or ""), scale, num, bool(pct), bool(unit)))
    return out


def unsupported_figures(bullet: str, source: str) -> list[str]:
    """Figures in `bullet` that can't be found in `source`. A figure is supported if
    the source has the same number, or one that rounds to it at the bullet's
    precision in the bullet's unit ("£40.3m" supports "£40m"; "£1,234 million"
    supports "£1.2bn"). Small bare integers (1-9) are ignored: sources often
    spell them out ("three new contracts")."""
    src = _figures(source)
    bad = []
    for value, dec, scale, num, pct, has_unit in _figures(bullet):
        if not pct and not has_unit and num.is_integer() and num < 10:
            continue
        ok = False
        for s_value, _, _, s_num, s_pct, _ in src:
            if pct != s_pct and (pct or s_pct):
                continue   # 18% is not supported by "18 stores"
            if abs(s_value - value) <= 1e-9 * max(1.0, abs(value)) or s_num == num:
                ok = True
                break
            if round(s_value / scale, dec) == round(num, dec):
                ok = True
                break
        if not ok:
            m = f"{num:g}" + ("%" if pct else "")
            bad.append(m)
    return bad


def _bullet_source(a, content: str) -> str:
    return f"{a.headline}\n{a.snippet}\n{content}"


state._BART_PIPE = None


def _load_bart(cfg: settings.Config):
    """Lazily load the local BART pipeline. transformers/torch are imported here
    (not at module top) so the rest of the tool runs without them."""
    if state._BART_PIPE is not None:
        return state._BART_PIPE
    from transformers import pipeline, AutoTokenizer, AutoModelForSeq2SeqLM
    src = cfg.bart_model_dir
    if not os.path.isfile(os.path.join(src, "config.json")):
        print(f"      BART: no local model at {src} — using {cfg.bart_hub_id} from the hub")
        src = cfg.bart_hub_id
    tok = AutoTokenizer.from_pretrained(src)
    model = AutoModelForSeq2SeqLM.from_pretrained(src)
    state._BART_PIPE = pipeline("summarization", model=model, tokenizer=tok)
    return state._BART_PIPE


def _bart_summarise(text: str, cfg: settings.Config) -> str:
    """Summarise article text into one short bullet with local BART."""
    pipe = _load_bart(cfg)
    clipped = " ".join((text or "").split()[: cfg.bart_max_input_words])
    out = pipe(clipped, max_length=cfg.bart_max_tokens, min_length=cfg.bart_min_tokens,
               do_sample=False, truncation=True, no_repeat_ngram_size=3)
    return patterns._tidy_bullet(out[0]["summary_text"])


def _openai_chat(client, model: str, prompt: str, cfg: settings.Config, max_tokens: int, schema: dict | None) -> str:
    kw = dict(model=model, max_tokens=max_tokens, timeout=cfg.llm_timeout,
              messages=[{"role": "user", "content": prompt}])
    if schema is not None:
        # Constrained decoding: the server can only emit JSON matching `schema`.
        # Supported by OpenAI and by Ollama >= 0.5; anything that rejects it is
        # retried once as a plain call (the parser still validates the answer).
        name = schema.get("title", "answer")
        body = {k: v for k, v in schema.items() if k != "title"}
        try:
            r = client.chat.completions.create(**kw, response_format={
                "type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": body}})
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            if e.__class__.__name__ in ("APITimeoutError", "APIConnectionError"):
                raise
    r = client.chat.completions.create(**kw)
    return (r.choices[0].message.content or "").strip()


class _LLMHealth:
    """Circuit breaker for the LLM. Timeouts and connection errors in a row trip
    it, after which calls fail instantly for the rest of the run (and bullets use
    the article's own sentences) instead of each story waiting out the timeout."""
    def __init__(self):
        self._lock = threading.Lock()
        self.failures = 0
        self.tripped = False

    def ok(self):
        with self._lock:
            self.failures = 0

    def fail(self, cfg: settings.Config):
        with self._lock:
            self.failures += 1
            if not self.tripped and self.failures >= max(1, cfg.llm_max_failures):
                self.tripped = True
                print(f"      LLM failed {self.failures} times in a row — switching it off for the rest "
                      f"of this run (bullets will use the articles' own sentences)")
                state._REPORT.warn(f"LLM switched off after {self.failures} consecutive failures")


state._LLM_STATE = _LLMHealth()


def _is_timeout(e: Exception) -> bool:
    return isinstance(e, TimeoutError) or "Timeout" in e.__class__.__name__


def _is_unreachable(e: Exception) -> bool:
    return (_is_timeout(e) or isinstance(e, (ConnectionError, urllib.error.URLError))
            or e.__class__.__name__ in ("APIConnectionError", "ConnectError"))


def warm_up_llm(cfg: settings.Config) -> threading.Thread | None:
    """Ask Ollama to load the model now (in the background) and keep it loaded,
    so the first summary doesn't pay a 30-60 s cold start. Best effort."""
    if cfg.llm_provider.lower() != "ollama":
        return None
    base = re.sub(r"/v1/?$", "", cfg.ollama_base_url.rstrip("/"))
    body = json.dumps({"model": cfg.ollama_model, "keep_alive": cfg.ollama_keep_alive}).encode()

    def go():
        try:
            req = urllib.request.Request(base + "/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=max(cfg.llm_timeout, 120)).read()
        except Exception:
            pass   # Ollama not running: the doctor / the first real call will say so

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _llm_complete(prompt: str, cfg: settings.Config, max_tokens: int = 160, schema: dict | None = None) -> str:
    """One LLM call with a hard timeout, one retry on timeout, and the circuit
    breaker. Raises on failure (callers fall back to non-LLM output)."""
    if state._LLM_STATE.tripped:
        raise RuntimeError("LLM switched off for this run after repeated failures")
    try:
        out = _llm_call(prompt, cfg, max_tokens, schema)
    except Exception as e:
        if _is_timeout(e) and cfg.llm_retry_on_timeout:
            try:
                out = _llm_call(prompt, replace(cfg, llm_timeout=cfg.llm_timeout * 2), max_tokens, schema)
                state._LLM_STATE.ok()
                return out
            except Exception as e2:
                e = e2
        if _is_unreachable(e):
            state._LLM_STATE.fail(cfg)
        raise e
    state._LLM_STATE.ok()
    return out


def _llm_call(prompt: str, cfg: settings.Config, max_tokens: int = 160, schema: dict | None = None) -> str:
    """Single provider call. With `schema`, the answer is constrained to that JSON
    schema where the provider supports it (Ollama, OpenAI)."""
    p = cfg.llm_provider.lower()
    if p == "bart":
        raise RuntimeError("bart provider is summarise-only; use _bart_summarise")
    if p == "ollama":
        if OpenAI is None:
            raise RuntimeError("openai package not installed")
        client = OpenAI(base_url=cfg.ollama_base_url, api_key="ollama",
                        timeout=cfg.llm_timeout, max_retries=0)
        return _openai_chat(client, cfg.ollama_model, prompt, cfg, max_tokens, schema)
    if p == "openai":
        if OpenAI is None:
            raise RuntimeError("openai package not installed")
        if not sources.env_key("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set")
        client = OpenAI(api_key=sources.env_key("OPENAI_API_KEY"),
                        timeout=cfg.llm_timeout, max_retries=0)
        return _openai_chat(client, cfg.openai_model, prompt, cfg, max_tokens, schema)
    if p == "anthropic":
        if anthropic is None:
            raise RuntimeError("anthropic package not installed")
        if not sources.env_key("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        client = anthropic.Anthropic(api_key=sources.env_key("ANTHROPIC_API_KEY"),
                                     timeout=cfg.llm_timeout, max_retries=0)
        r = client.messages.create(model=cfg.anthropic_model, max_tokens=max_tokens,
                                   messages=[{"role": "user", "content": prompt}])
        return r.content[0].text.strip()
    raise RuntimeError(f"unknown provider '{p}'")


def summarise_bullet(a: models.Article, company: models.Company, cfg: settings.Config) -> str:
    if a.kind == "filing":
        return _fallback_bullet(a)
    content = a.body if (a.body and len(a.body) >= 120) else a.snippet
    if not content or len(content) < 60:
        return _fallback_bullet(a)
    provider, model, key = cfg.llm_provider.lower(), health._model_name(cfg), extraction._orig_key(a)
    source = _bullet_source(a, content[: cfg.max_article_chars])

    def checked(text: str, fresh: bool) -> str:
        """Number check, then cache. A bullet with a figure the article doesn't
        contain is replaced by the article's own sentences and never cached."""
        if not text:
            state._REPORT.count("bullets: fallback (empty answer)")
            return _fallback_bullet(a)
        bad = unsupported_figures(text, source) if cfg.verify_figures else []
        if bad:
            print(f"      number check: {', '.join(bad[:3])} not in the article — "
                  f"using the article's own sentences ({a.headline[:50]}...)")
            state._REPORT.count("bullets: replaced by number check")
            return _fallback_bullet(a)
        if fresh:
            state._CACHE.put_bullet(key, provider, model, text)
        state._REPORT.count("bullets: AI (cached)" if not fresh else "bullets: AI")
        return text

    cached = state._CACHE.get_bullet(key, provider, model)
    if cached:
        return checked(cached, fresh=False)
    if provider == "bart":
        try:
            text = _bart_summarise(content[: cfg.max_article_chars], cfg)
        except Exception as e:
            print(f"      BART bullet failed ({e.__class__.__name__}) — using fallback")
            state._REPORT.count("bullets: fallback (model unavailable)")
            return _fallback_bullet(a)
        return checked(text, fresh=True)
    prompt = (
        f'Summarise this news about "{company.name}" as ONE concise bullet point '
        f"(one or two sentences, max ~40 words) for an investor deciding whether the "
        f"stock is worth holding, and for competitive intelligence. State only what is "
        f"materially relevant: earnings, revenue, margins, M&A, contracts/orders, "
        f"capacity or market expansion, guidance, leadership, regulatory or competitive "
        f"positioning. Ignore marketing, discounts, promotions and consumer offers. No "
        f"preamble, no 'the article', no links, no bullet character. Use only figures "
        f"(amounts, percentages, dates) exactly as they appear in the text; never "
        f"calculate, convert or estimate a number.\n\n"
        f"HEADLINE: {a.headline}\n\n{content[: cfg.max_article_chars]}"
    )
    try:
        text = patterns._tidy_bullet(_llm_complete(prompt, cfg))
    except Exception as e:
        print(f"      LLM bullet failed ({e.__class__.__name__}) — using fallback")
        state._REPORT.count("bullets: fallback (LLM unreachable)")
        return _fallback_bullet(a)   # fallbacks are never cached, so a later run retries the LLM
    return checked(text, fresh=True)
