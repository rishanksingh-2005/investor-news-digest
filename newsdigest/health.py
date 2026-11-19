"""Run health report, log tee, and small shared helpers (HTTP retry, safe calls)."""

from __future__ import annotations

import time
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import state


# ============================================================================
# Run health report
#
# Every source and step records what happened. At the end of a run this is
# printed as a short report, written to the log, and turned into an exit code a
# scheduler can act on:  0 = OK, 1 = failed (nothing usable produced),
# 2 = published but degraded (a source or the LLM was failing).
# ============================================================================
EXIT_OK, EXIT_FAILED, EXIT_DEGRADED = 0, 1, 2


class RunReport:
    def __init__(self):
        self._lock = threading.Lock()
        self.sources: dict = {}       # name -> {"ok": n, "error": n, "skipped": n, "errors": [...]}
        self.counts: dict = {}        # free-form counters ("bullets: AI", ...)
        self.fatal = ""               # reason the run produced nothing usable
        self.warnings: list = []

    def source(self, name: str, outcome: str, detail: str = "") -> None:
        with self._lock:
            s = self.sources.setdefault(name, {"ok": 0, "error": 0, "skipped": 0, "errors": []})
            s[outcome] += 1
            if detail and outcome == "error" and detail not in s["errors"] and len(s["errors"]) < 3:
                s["errors"].append(detail[:120])

    def count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + n

    def warn(self, msg: str) -> None:
        with self._lock:
            if msg not in self.warnings:
                self.warnings.append(msg)

    def all_warnings(self) -> list:
        w = list(self.warnings)
        for name, s in sorted(self.sources.items()):
            if s["error"] > s["ok"]:   # failing more often than it works
                w.append(f"{name} failed {s['error']} of {s['ok'] + s['error']} times"
                         + (f" ({s['errors'][0]})" if s["errors"] else ""))
        bullets = sum(v for k, v in self.counts.items() if k.startswith("bullets:"))
        down = self.counts.get("bullets: fallback (LLM unreachable)", 0)
        if bullets and down * 2 >= bullets:
            w.append(f"LLM unreachable for {down} of {bullets} bullets (plain extracts used)")
        return w

    def exit_code(self) -> int:
        if self.fatal:
            return EXIT_FAILED
        return EXIT_DEGRADED if self.all_warnings() else EXIT_OK

    def lines(self) -> list[str]:
        out = ["Run report"]
        for name, s in sorted(self.sources.items()):
            bits = [f"{s['ok']} ok"] + [f"{s[k]} {k}" for k in ("error", "skipped") if s[k]]
            out.append(f"  {name:<34} {', '.join(bits)}")
        for k, v in sorted(self.counts.items()):
            out.append(f"  {k:<34} {v}")
        status = {EXIT_OK: "OK", EXIT_FAILED: "FAILED", EXIT_DEGRADED: "DEGRADED"}[self.exit_code()]
        out.append(f"  Status: {status}" + (f" ({self.fatal})" if self.fatal else ""))
        out += [f"  ! {w}" for w in self.all_warnings()]
        return out


# Report for the current (or last) run; main() reads its exit code.
state._REPORT = RunReport()


class _Tee:
    """stdout that also writes to the run log."""
    def __init__(self, stream, log):
        self.stream, self.log, self._lock = stream, log, threading.Lock()

    def write(self, s):
        with self._lock:
            self.stream.write(s)
            if not self.log.closed:
                self.log.write(s)
        return len(s)

    def flush(self):
        self.stream.flush()
        if not self.log.closed:
            self.log.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _model_name(cfg) -> str:
    p = cfg.llm_provider.lower()
    return {"ollama": cfg.ollama_model, "openai": cfg.openai_model,
            "anthropic": cfg.anthropic_model, "bart": cfg.bart_model_dir}.get(p, p)


def _urlopen_retry(req, timeout: float = 20, attempts: int = 2, backoff: float = 3.0) -> bytes:
    """GET with one polite retry on rate-limit / server errors (429, 5xx)."""
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise


def _driver_alive(driver) -> bool:
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def _safe(fn, default=""):
    try:
        return fn()
    except Exception:
        return default
