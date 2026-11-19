"""Step 2 routing: cache -> plain HTTP -> Chrome pool."""
import io

import pytest


class _HTMLResp(io.BytesIO):
    """Minimal urlopen() response with headers and a post-redirect URL."""

    def __init__(self, body: str, url: str, ctype: str = "text/html; charset=utf-8"):
        super().__init__(body.encode("utf-8"))
        self._url = url
        import email.message
        self.headers = email.message.Message()
        self.headers["Content-Type"] = ctype

    def geturl(self):
        return self._url


@pytest.fixture
def http_pages(nd, monkeypatch):
    """Serve fixed HTML per URL substring; record every URL fetched."""
    def install(pages: dict):
        calls = []

        def urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            calls.append(url)
            for needle, (body, final, *ctype) in pages.items():
                if needle in url:
                    return _HTMLResp(body, final, *ctype)
            raise OSError(f"no route for {url}")

        monkeypatch.setattr(nd.urllib.request, "urlopen", urlopen)
        return calls
    return install


@pytest.fixture
def browser(nd, monkeypatch):
    """Replace the Chrome pool: record what it was asked to open and 'read' each one."""
    opened = []

    def fake(articles, cfg):
        for a in articles:
            opened.append(a)
            a.final_url = "https://publisher.example/resolved"
            a.body = "Resolved by the browser. " * 30
            a.authentic = True

    monkeypatch.setattr(nd, "_extract_with_browsers", fake)
    return opened


def test_needs_browser_only_for_google_redirects(nd, make_article):
    assert nd._needs_browser(make_article(url="https://news.google.com/rss/articles/CBMi1"))
    assert not nd._needs_browser(make_article(url="https://news.google.com/rss/articles/CBMi1",
                                              final_url="https://www.reuters.com/a"))
    assert not nd._needs_browser(make_article(url="https://finnhub.io/api/news?id=1"))


def test_extract_http_reads_article_and_follows_redirect(nd, cfg, make_article, http_pages, fixture_text):
    http_pages({"finnhub.io": (fixture_text("article.html"), "https://www.reuters.com/acme")})
    a = make_article(url="https://finnhub.io/api/news?id=1")
    assert nd.extract_http(a, cfg)
    assert a.final_url == "https://www.reuters.com/acme"
    assert a.authentic and "£412m" in a.body


def test_extract_http_rejects_non_html_and_stubs(nd, cfg, make_article, http_pages):
    http_pages({"pdf.example": ("%PDF-1.7", "https://pdf.example/x", "application/pdf"),
                "stub.example": ("<p>Subscribe to read.</p>", "https://stub.example/x")})
    assert not nd.extract_http(make_article(url="https://pdf.example/x"), cfg)
    assert not nd.extract_http(make_article(url="https://stub.example/x"), cfg)
    assert not nd.extract_http(make_article(url="https://unreachable.example/x"), cfg)
    assert not nd.extract_http(make_article(url="https://news.google.com/rss/articles/X"), cfg)


def test_extract_all_routes_each_article(nd, cfg, make_article, http_pages, browser, fixture_text):
    http_pages({"reuters.com/ok": (fixture_text("article.html"), "https://www.reuters.com/ok"),
                "paywalled.example": ("<p>Subscribe to read.</p>", "https://paywalled.example/x")})
    google = make_article(url="https://news.google.com/rss/articles/CBMi1")
    direct = make_article(url="https://www.reuters.com/ok")
    paywalled = make_article(url="https://paywalled.example/x")
    filing = make_article(url="https://www.sec.gov/x", kind="filing")

    stats = nd.extract_all([google, direct, paywalled, filing], cfg)

    assert browser == [google, paywalled]          # HTTP read 'direct'; Chrome gets the rest
    assert stats == {"cached": 0, "http": 1, "browser": 2, "unreadable": 0, "redated": 0}
    assert direct.authentic and filing.authentic


def test_extract_all_can_disable_fast_path(nd, cfg, make_article, http_pages, browser):
    calls = http_pages({})
    cfg.http_fast_path = False
    a = make_article(url="https://www.reuters.com/ok")
    nd.extract_all([a], cfg)
    assert calls == [] and browser == [a]


def test_extract_all_second_run_comes_from_cache(nd, cfg, cache, make_article, http_pages, browser,
                                                 fixture_text):
    calls = http_pages({"reuters.com/ok": (fixture_text("article.html"), "https://www.reuters.com/ok")})
    nd.extract_all([make_article(url="https://news.google.com/rss/articles/CBMi1"),
                    make_article(url="https://www.reuters.com/ok")], cfg)
    assert len(calls) == 1 and len(browser) == 1

    again = [make_article(url="https://news.google.com/rss/articles/CBMi1"),
             make_article(url="https://www.reuters.com/ok")]
    stats = nd.extract_all(again, cfg)
    assert stats["cached"] == 2
    assert len(calls) == 1 and len(browser) == 1          # no new HTTP or Chrome work
    assert again[0].final_url == "https://publisher.example/resolved" and again[0].authentic
    assert "£412m" in again[1].body


def test_browser_pool_survives_chrome_failing_to_start(nd, cfg, make_article, monkeypatch, capsys):
    def no_chrome(cfg):
        raise RuntimeError("chrome not installed")

    monkeypatch.setattr(nd, "build_driver", no_chrome)
    arts = [make_article(url=f"https://news.google.com/rss/articles/{i}") for i in range(5)]
    nd._extract_with_browsers(arts, cfg)                  # must not raise or hang
    assert not any(a.authentic for a in arts)
    assert "Chrome failed to start" in capsys.readouterr().out


def test_browser_pool_uses_parallel_drivers_and_quits_them(nd, cfg, make_article, monkeypatch):
    built, quit_ = [], []

    class FakeDriver:
        current_url = "https://publisher.example/x"

        def quit(self):
            quit_.append(self)

    def build(cfg):
        d = FakeDriver()
        built.append(d)
        return d

    def read(driver, a, cfg):
        a.body, a.authentic = "x", True

    monkeypatch.setattr(nd, "build_driver", build)
    monkeypatch.setattr(nd, "extract_body", read)
    cfg.browser_workers = 3
    arts = [make_article(url=f"https://news.google.com/rss/articles/{i}") for i in range(7)]
    nd._extract_with_browsers(arts, cfg)
    assert all(a.authentic for a in arts)
    assert 1 <= len(built) <= 3 and sorted(map(id, quit_)) == sorted(map(id, built))
