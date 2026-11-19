"""SQLite cache: storage, expiry, and how each pipeline step uses it."""
import time

import pytest


# --- the Cache class itself ---------------------------------------------------------

def test_disabled_cache_is_a_no_op(nd):
    c = nd.Cache(None)
    assert not c.enabled
    c.put_feed("u", b"x")
    c.put_article("k", "f", "body", True)
    c.put_bullet("k", "p", "m", "b")
    c.mark_published(["k"], "Co", "2026-11-19")
    assert c.get_feed("u", 999) is None
    assert c.get_article("k", 999, 999) is None
    assert c.get_bullet("k", "p", "m") is None
    assert c.was_published("k") is False
    c.close()


def test_feed_ttl(nd, cache, monkeypatch):
    cache.put_feed("https://feed", b"<rss/>")
    assert cache.get_feed("https://feed", 60) == b"<rss/>"
    later = time.time() + 120
    monkeypatch.setattr(nd.time, "time", lambda: later)
    assert cache.get_feed("https://feed", 60) is None


def test_articles_failed_entries_expire_sooner(nd, cache, monkeypatch):
    cache.put_article("good", "https://pub/good", "body", True)
    cache.put_article("bad", "https://pub/bad", "", False)
    day = 86400
    assert cache.get_article("good", 30 * day, day)["body"] == "body"
    assert cache.get_article("bad", 30 * day, day)["authentic"] is False
    later = time.time() + 2 * day
    monkeypatch.setattr(nd.time, "time", lambda: later)
    assert cache.get_article("good", 30 * day, day) is not None    # still fresh
    assert cache.get_article("bad", 30 * day, day) is None         # unreadable page gets retried


def test_bullets_are_per_provider_and_model(cache):
    cache.put_bullet("story", "ollama", "llama3.2:3b", "Bullet A.")
    assert cache.get_bullet("story", "ollama", "llama3.2:3b") == "Bullet A."
    assert cache.get_bullet("story", "ollama", "other-model") is None
    assert cache.get_bullet("story", "openai", "llama3.2:3b") is None


def test_published_and_persistence(nd, tmp_path):
    path = str(tmp_path / "c.sqlite")
    c = nd.Cache(path)
    c.mark_published(["a", "", "b"], "Acme", "2026-11-19")
    c.close()
    c2 = nd.Cache(path)                    # reopened: survives across runs
    assert c2.was_published("x", "b")
    assert not c2.was_published("x", "")
    c2.close()


# --- pipeline steps using the cache ---------------------------------------------------

def test_google_news_feed_is_served_from_cache(nd, cfg, cache, fake_urlopen, fixture_text):
    calls = fake_urlopen({"news.google.com": [fixture_text("google_news.xml")]})
    first = nd._google_news_rss("Acme", cfg, "Google News")
    second = nd._google_news_rss("Acme", cfg, "Google News")
    assert len(calls) == 1
    assert [a.headline for a in first] == [a.headline for a in second]


def test_bad_feed_responses_are_not_cached(nd, cfg, cache, fake_urlopen):
    calls = fake_urlopen({"news.google.com": ["<html>not rss</html>"]})
    assert nd._google_news_rss("Acme", cfg, "Google News") == []
    assert nd._google_news_rss("Acme", cfg, "Google News") == []
    assert len(calls) == 2


def test_llm_bullets_cached_but_fallbacks_are_not(nd, cfg, cache, make_article, monkeypatch):
    co = nd.Company(name="Acme")
    body = "Acme raised guidance to £1.2bn after a record half. " * 4
    calls = []

    def llm(prompt, cfg, max_tokens=160):
        calls.append(1)
        return "Acme lifted FY guidance to £1.2bn"

    monkeypatch.setattr(nd, "_llm_complete", llm)
    a = make_article(url="https://www.reuters.com/a", body=body)
    assert nd.summarise_bullet(a, co, cfg) == "Acme lifted FY guidance to £1.2bn."
    assert nd.summarise_bullet(make_article(url="https://www.reuters.com/a", body=body), co, cfg) \
        == "Acme lifted FY guidance to £1.2bn."
    assert len(calls) == 1

    def down(*a, **k):
        raise ConnectionError("ollama down")

    monkeypatch.setattr(nd, "_llm_complete", down)
    b = make_article(url="https://www.reuters.com/b", body=body)
    nd.summarise_bullet(b, co, cfg)                     # fallback bullet
    assert cache.get_bullet(nd._orig_key(b), cfg.llm_provider, nd._model_name(cfg)) is None


def test_topic_grouping_answer_is_cached(nd, cfg, cache, make_article, monkeypatch):
    import json
    nd._CACHE = cache
    co = nd.Company(name="Acme Widgets Plc")
    reply = json.dumps([{"heading": "Contracts", "members": [1, 2], "summary": "Acme won a contract."}])
    calls = []

    def llm(prompt, cfg, max_tokens=160):
        calls.append(prompt)
        return reply

    monkeypatch.setattr(nd, "_llm_complete", llm)
    arts = lambda: [make_article(bullet="Acme won a contract."), make_article(bullet="Acme wins deal.")]  # noqa: E731
    first = nd._llm_organise(arts(), co, cfg)
    second = nd._llm_organise(arts(), co, cfg)
    assert len(calls) == 1
    assert [(h, s) for h, s, _ in first] == [(h, s) for h, s, _ in second]


@pytest.mark.parametrize("reply", ["no json here", '[{"members": [1]}]', '[{"heading": "x"}]', "[[1, 2]]"])
def test_unusable_grouping_is_remembered_and_falls_back(nd, cfg, cache, make_article, monkeypatch, reply):
    nd._CACHE = cache
    co = nd.Company(name="Acme Widgets Plc")
    calls = []
    monkeypatch.setattr(nd, "_llm_complete", lambda *a, **k: calls.append(1) or reply)
    arts = lambda: [make_article(bullet="Acme won a contract."), make_article(bullet="Acme named a CFO.")]  # noqa: E731
    for _ in range(2):
        with pytest.raises(ValueError):
            nd._llm_organise(arts(), co, cfg)
        assert nd.organise(arts(), co, cfg)   # heuristic fallback still produces the digest
    assert len(calls) == 1


def test_llm_outage_during_grouping_is_retried_next_run(nd, cfg, cache, make_article, monkeypatch):
    nd._CACHE = cache
    co = nd.Company(name="Acme Widgets Plc")
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise TimeoutError("ollama not responding")

    monkeypatch.setattr(nd, "_llm_complete", down)
    arts = lambda: [make_article(bullet="Acme won a contract."), make_article(bullet="Acme named a CFO.")]  # noqa: E731
    nd.organise(arts(), co, cfg)
    nd.organise(arts(), co, cfg)
    assert len(calls) == 2
