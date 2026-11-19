"""Shared fixtures.

The code lives in the ``newsdigest`` package, one module per pipeline stage.
Tests use ``nd``: a single view over every module, so ``nd.filter_articles``
or ``monkeypatch.setattr(nd, "_llm_complete", fake)`` work without knowing
which module a name lives in. Each name is defined in exactly one module, and
patching ``nd`` patches that module, so every caller sees the fake.

Every test runs fully offline: network calls are replaced with canned
responses from ``tests/fixtures`` via the ``fake_urlopen`` fixture.
"""
from __future__ import annotations

import ast
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

import newsdigest  # noqa: E402
from newsdigest import state as _state  # noqa: E402

_MODULE_NAMES = ["state", "patterns", "models", "settings", "watchlists", "configfile", "browser", "storage", "health",
                 "sources", "extraction", "filtering", "grouping", "summarising", "publishing", "delivery", "pipeline",
                 "cli"]
_MODULES = [getattr(newsdigest, m) for m in _MODULE_NAMES]


def _owners() -> dict:
    """name -> the module that defines it (top-level def / class / assignment)."""
    owners = {}
    for mod in _MODULES:
        for node in ast.parse(Path(mod.__file__).read_text(encoding="utf-8")).body:
            names = []
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
            for n in names:
                owners.setdefault(n, mod)
    for n in vars(_state):
        if not n.startswith("__"):
            owners[n] = _state
    return owners


class _View:
    """Read any name from the module that owns it; setting a name sets it there.
    No instance __dict__ (pytest's monkeypatch would read the old value from it)."""
    __slots__ = ()
    __file__ = str(ROOT / "news digest.py")
    _owner = _owners()

    def __getattr__(self, name):
        mod = self._owner.get(name)
        if mod is not None:
            return getattr(mod, name)
        for m in _MODULES:                       # stdlib / third-party modules they import
            if name in vars(m):
                return vars(m)[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        mod = self._owner.get(name)
        if mod is not None:
            setattr(mod, name, value)
            return
        # an imported name (e.g. the optional `anthropic` SDK): patch every module that has it
        holders = [m for m in _MODULES if name in vars(m)]
        if not holders:
            raise AttributeError(f"no module has {name!r}")
        for m in holders:
            setattr(m, name, value)


_ND = _View()
REAL_WARM_UP = newsdigest.summarising.warm_up_llm   # before any test stubs it


@pytest.fixture
def real_warm_up():
    return REAL_WARM_UP


@pytest.fixture
def nd(monkeypatch):
    """The news-digest module, with per-run global state reset for each test."""
    monkeypatch.setattr(_ND, "_FINNHUB_SYMBOL_CACHE", {})
    monkeypatch.setattr(_ND, "_FINNHUB_DISABLED", False)
    monkeypatch.setattr(_ND, "_SEC_UA_WARNED", False)
    monkeypatch.setattr(_ND, "_REPORT", _ND.RunReport())
    monkeypatch.setattr(_ND, "_LLM_STATE", _ND._LLMHealth())
    monkeypatch.setattr(_ND, "_GDELT_STATE", {"disabled": False, "rate_limited": 0})
    monkeypatch.setattr(_ND, "_GDELT_LAST", [0.0])
    monkeypatch.setattr(_ND, "warm_up_llm", lambda cfg: None)   # never call a real Ollama
    monkeypatch.setattr(_ND, "_BART_PIPE", None)
    monkeypatch.setattr(_ND, "_CACHE", _ND.Cache(None))       # caching off unless a test opts in
    monkeypatch.setattr(_ND.time, "sleep", lambda s: None)    # retries / polite delays: no waiting
    # Never let a test pick up real keys from a developer's .env.
    for var in ("FINNHUB_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SEC_USER_AGENT"):
        monkeypatch.delenv(var, raising=False)
    return _ND


@pytest.fixture
def cfg(nd):
    """A Config that can never reach an LLM and never sleeps."""
    c = nd.Config()
    c.llm_provider = "offline-test"   # unknown provider -> _llm_complete raises -> fallbacks
    c.polite_delay = 0
    c.use_cache = False               # never touch the real digest_cache.sqlite
    return c


@pytest.fixture
def cache(nd, monkeypatch, tmp_path):
    """A real, empty SQLite cache in a temp folder, installed as the module cache."""
    c = nd.Cache(str(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(nd, "_CACHE", c)
    yield c
    c.close()


@pytest.fixture
def fixture_text():
    def read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")
    return read


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def fake_urlopen(nd, monkeypatch):
    """Route urllib.request.urlopen to canned responses.

    Usage: ``calls = fake_urlopen({"efts.sec.gov": [500, payload_bytes]})`` —
    each value is a list consumed in order; an int is raised as that HTTP
    status, bytes/str/dict are returned as the body. Unmatched URLs fail the
    test, so nothing can slip out to the real network.
    """
    def install(routes: dict):
        queues = {k: list(v) for k, v in routes.items()}
        calls: list[str] = []

        def urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            calls.append(url)
            for needle, queue in queues.items():
                if needle in url:
                    item = queue.pop(0) if len(queue) > 1 else queue[0]
                    if isinstance(item, int):
                        raise urllib.error.HTTPError(url, item, f"HTTP {item}", {}, None)
                    if isinstance(item, dict) or isinstance(item, list):
                        item = json.dumps(item)
                    if isinstance(item, str):
                        item = item.encode("utf-8")
                    return _Resp(item)
            raise AssertionError(f"unexpected network call in test: {url}")

        monkeypatch.setattr(nd.urllib.request, "urlopen", urlopen)
        return calls
    return install


@pytest.fixture
def make_article(nd):
    def make(**kw):
        kw.setdefault("headline", "Acme Widgets wins major contract")
        kw.setdefault("url", "https://www.reuters.com/business/acme-contract")
        return nd.Article(**kw)
    return make
