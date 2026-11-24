"""Article-body extraction and text clean-up."""


def test_extract_article_text_reads_article_container_only(nd, fixture_text):
    text = nd._extract_article_text(fixture_text("article.html"), "https://example.com/a")
    assert "orders from industrial customers rose 18% to £412m" in text
    assert "interim dividend of 4.2p" in text
    # scripts, nav, aside and footer are stripped
    assert "tracking" not in text
    assert "Most read" not in text
    assert "All rights reserved" not in text


def test_extracted_fixture_counts_as_authentic(nd, fixture_text):
    body = nd._clean_body(nd._extract_article_text(fixture_text("article.html"), "https://example.com/a"))
    assert nd._is_authentic(body)


def test_is_authentic_rejects_short_and_paywall_stubs(nd):
    assert not nd._is_authentic("")
    assert not nd._is_authentic("Too short. Really. Short. Text.")
    stub = ("Subscribe to continue reading. " * 10) + ("You have reached your limit. " * 10)
    assert not nd._is_authentic(stub)
    no_sentences = "word " * 200
    assert not nd._is_authentic(no_sentences)


def test_clean_body_drops_boilerplate_lines_but_keeps_prose(nd):
    raw = "\n".join([
        "Advertisement",
        "Subscribe to our newsletter",
        "By Jane Reporter",
        "5 min read",
        "Acme Widgets raised its guidance after a record half, with orders up 18% to £412m.",
        "",
        "",
        "",
        "Share this article",
    ])
    out = nd._clean_body(raw)
    assert out == "Acme Widgets raised its guidance after a record half, with orders up 18% to £412m."


def test_clean_body_never_drops_long_paragraphs_with_junk_words(nd):
    long_para = ("Analysts said readers who subscribe to the company's services saw strong growth, "
                 "and revenue rose sharply across every region the group operates in this year.")
    assert len(long_para) >= 120
    assert nd._clean_body(long_para) == long_para


def test_clean_body_empty(nd):
    assert nd._clean_body("") == ""
    assert nd._clean_body(None) == ""


def test_tidy_bullet_normalises_text(nd):
    assert nd._tidy_bullet("- revenue rose ,  sharply") == "revenue rose, sharply."
    assert nd._tidy_bullet("• Profit up 10%!!!") == "Profit up 10%!"
    assert nd._tidy_bullet("Deal agreed [1]") == "Deal agreed."
    assert nd._tidy_bullet("Strong quarter **") == "Strong quarter."
    assert nd._tidy_bullet("Already ends.") == "Already ends."
    assert nd._tidy_bullet("") == ""
    assert nd._tidy_bullet(None) == ""


def test_extract_facts_pulls_money_percent_and_dates(nd):
    facts = nd.extract_facts("Revenue hit £1.2bn, up 14%, on 4 Nov 2026; costs were $300m (2026-11-04).")
    assert "£1.2bn" in facts["money"]
    assert any(m.startswith("$300") for m in facts["money"])
    assert "14%" in facts["percent"]
    assert "4 Nov 2026" in facts["dates"]
    assert "2026-11-04" in facts["dates"]


ROBOT_CHECK = ("Please confirm you're not a robot. To keep reading, tick the box below. Make sure your "
               "browser allows JavaScript and cookies and isn't blocking them. Read our Terms of Service "
               "and Cookie Policy for more. Need help? Contact our support team with the reference below. "
               "Block reference: 8c1f-22aa. Thank you for your patience while we check your connection. "
               "This usually takes a few seconds. You will be sent to the page you asked for afterwards.")


def test_is_authentic_rejects_robot_check_pages(nd):
    """A publisher's 'are you a robot?' page is long enough and has enough
    sentences to pass the other checks: it must still not count as an article."""
    assert len(ROBOT_CHECK) > 400
    assert not nd._is_authentic(ROBOT_CHECK)
    assert not nd._is_authentic(ROBOT_CHECK.replace("you're not a robot", "verify you are human"))


def test_long_article_quoting_a_robot_check_phrase_is_still_authentic(nd, fixture_text):
    body = nd._clean_body(nd._extract_article_text(fixture_text("article.html"), "https://example.com/a"))
    padded = body + " Customers said the old site kept asking them to prove they are not a robot." + " More text." * 200
    assert nd._is_authentic(padded)
