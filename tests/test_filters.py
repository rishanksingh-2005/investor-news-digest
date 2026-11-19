"""Relevance, recency and quality gates (step 3)."""
from datetime import datetime, timedelta, timezone

import pytest


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# --- blocklist ---------------------------------------------------------------

@pytest.mark.parametrize("source,url,final,blocked", [
    ("Reuters", "https://www.reuters.com/a", "", False),
    ("Simply Wall St", "https://news.google.com/rss/x", "", True),      # spaces in feed name
    ("Yahoo Finance", "https://news.google.com/rss/x", "https://simplywall.st/a", True),  # real URL
    ("MarketBeat", "https://news.google.com/rss/x", "", True),
    ("Motley Fool", "https://news.google.com/rss/x", "", True),
    ("Daily Mail", "https://www.dailymail.co.uk/x", "", True),
    ("Financial Times", "https://www.ft.com/content/x", "", False),
])
def test_is_blocklisted(nd, make_article, source, url, final, blocked):
    a = make_article(source=source, url=url, final_url=final)
    assert nd.is_blocklisted(a) is blocked


# --- relevance ---------------------------------------------------------------

def test_is_relevant_needs_company_in_lead_and_a_material_keyword(nd, make_article):
    co = nd.Company(name="Acme Widgets Plc")
    assert nd.is_relevant(make_article(headline="Acme Widgets wins major contract"), co)
    # no material keyword anywhere
    assert not nd.is_relevant(make_article(headline="Acme Widgets hosts open day"), co)
    # company only buried in the body, single token match -> rejected
    buried = make_article(headline="Industry revenue climbs", body="... widgets ...")
    assert not nd.is_relevant(buried, co)
    # empty article
    assert not nd.is_relevant(make_article(headline="", snippet="", body=""), co)


def test_filings_are_always_relevant(nd, make_article):
    co = nd.Company(name="Acme Widgets Plc")
    f = make_article(headline="SEC 8-K filing — Other Name", kind="filing")
    assert nd.is_relevant(f, co)
    assert nd.strongly_relevant(f, co)


def test_strongly_relevant_uses_distinctive_token(nd, make_article):
    co = nd.Company(name="Douglas Dynamics, Inc.")
    assert nd.strongly_relevant(make_article(headline="Douglas Dynamics posts record revenue"), co)
    assert not nd.strongly_relevant(make_article(headline="Snowplow makers see record revenue"), co)


def test_strongly_relevant_generic_names_need_the_full_phrase(nd, make_article):
    # "North West" is only generic words -> must appear as a phrase
    co = nd.Company(name="North West Company Ltd")
    assert nd.strongly_relevant(make_article(headline="North West reports higher profit"), co)
    assert not nd.strongly_relevant(make_article(headline="West coast and north region sales rise"), co)


# --- recency -----------------------------------------------------------------

def test_is_recent(nd, cfg, make_article):
    assert nd.is_recent(make_article(published=_iso(1)), cfg)
    assert not nd.is_recent(make_article(published=_iso(cfg.recency_grace_days + 5)), cfg)
    assert nd.is_recent(make_article(published=""), cfg)            # unknown date is kept
    assert nd.is_recent(make_article(published="garbage"), cfg)
    assert nd.is_recent(make_article(published=_iso(400), kind="filing"), cfg)


def test_is_recent_handles_rfc822_and_naive_dates(nd, cfg, make_article):
    fresh = (datetime.now(timezone.utc) - timedelta(days=1))
    assert nd.is_recent(make_article(published=fresh.strftime("%a, %d %b %Y %H:%M:%S GMT")), cfg)
    assert nd.is_recent(make_article(published=fresh.replace(tzinfo=None).isoformat()), cfg)


# --- quality gates -------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "No specific news about the company this week.",
    "Revenue figures were not provided in the article.",
    "The report does not mention earnings.",
    "There is no information regarding guidance.",
])
def test_is_no_news_catches_llm_hedging(nd, text):
    assert nd.is_no_news(text)


def test_is_no_news_passes_real_news(nd):
    assert not nd.is_no_news("Acme raised guidance to £1.2bn after orders rose 18%.")


def test_price_noise_dropped_unless_real_event(nd):
    assert nd.is_price_noise("Acme shares rose 5% to a 52-week high; market cap now £2bn.")
    assert not nd.is_price_noise("Acme shares rose 5% after it announced the acquisition of Beta Ltd.")
    assert not nd.is_price_noise("Acme opens new plant in Ohio.")


def test_promotional_and_admin_filler(nd):
    assert nd.is_promotional("Get 20% off all holidays booked this week")
    assert nd.is_promotional("Use promo code SUMMER for £50 off")
    assert not nd.is_promotional("Shares trade at a discount to net asset value")
    assert nd.is_admin_filler("Acme Plc - Total Voting Rights")
    assert nd.is_admin_filler("Transaction in own shares")
    assert not nd.is_admin_filler("Acme Plc - Half-year results")


def test_importance_score_and_materiality(nd, cfg):
    rich = "Acme agreed a £300m acquisition, lifting revenue guidance by 5%."
    assert nd.importance_score(rich) >= 4
    assert nd.importance_score("A nice day out.") == 0
    assert nd.is_material(rich, cfg)
    assert not nd.is_material("A nice day out.", cfg)
    assert not nd.is_material("No specific news, but revenue £1m.", cfg)   # hedging wins


def test_filter_articles_end_to_end(nd, cfg, make_article):
    co = nd.Company(name="Acme Widgets Plc")
    good = make_article(headline="Acme Widgets wins £40m defence contract", published=_iso(2))
    blocked = make_article(headline="Acme Widgets wins contract", source="MarketBeat", published=_iso(2))
    stale = make_article(headline="Acme Widgets wins contract", published=_iso(90))
    promo = make_article(headline="Acme Widgets launch: 20% off all widgets", published=_iso(1))
    offtopic = make_article(headline="Rival Corp wins contract", published=_iso(1))
    filing = make_article(headline="SEC 8-K filing — Acme", kind="filing")
    kept = nd.filter_articles([good, blocked, stale, promo, offtopic, filing], co, cfg)
    assert kept == [good, filing]
