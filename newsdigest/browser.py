"""Chrome via Selenium, stripped down to text (no images, media, ads or trackers)."""

from __future__ import annotations

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from . import patterns, settings


# ============================================================================
# Selenium driver  (text-only: images / media / ads / trackers blocked)
# ============================================================================
def build_driver(cfg: settings.Config):
    opts = Options()
    if cfg.headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,1000")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--mute-audio")
    opts.add_argument("--blink-settings=imagesEnabled=false")   # no images
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    # Block ad/tracker/video hosts at the network layer -> faster, text-only.
    host_rules = ",".join(f"MAP {h} 127.0.0.1" for h in patterns.AD_HOSTS)
    opts.add_argument(f"--host-rules={host_rules}")
    # Disable images, plugins, popups, autoplay, notifications via content prefs.
    opts.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.plugins": 2,
        "profile.managed_default_content_settings.popups": 2,
        "profile.managed_default_content_settings.media_stream": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.autoplay": 2,
    })
    # "eager" = stop waiting once the DOM is ready (don't wait on trackers).
    opts.page_load_strategy = "eager"

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(cfg.page_load_timeout)
    try:
        driver.set_script_timeout(15)
    except Exception:
        pass
    return driver
