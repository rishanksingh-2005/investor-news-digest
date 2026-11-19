"""Guards against publishing secrets or client data by accident."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_has_no_values():
    """.env.example is committed: every KEY= line must stay empty. Real keys and
    contact details belong in .env, which is git-ignored."""
    filled = [ln.split("=", 1)[0] for ln in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
              if re.match(r"^[A-Z_]+=\s*\S", ln)]
    assert not filled, f".env.example has values for {filled}: move them to .env and leave the template empty"


def test_gitignore_covers_secrets_and_client_data():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "Company List.xlsx", "/watchlist.*", "digest_cache.sqlite*", "NLP/"):
        assert pattern in ignored, f"{pattern} missing from .gitignore"


def test_no_api_keys_in_tracked_text_files():
    """Nothing that looks like a live key in code, docs or config."""
    key_like = re.compile(r"(sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|"
                          r"(FINNHUB_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=\s*['\"]?[A-Za-z0-9_-]{16,})")
    skip = {".venv", "NLP", ".git", ".pytest_cache", ".ruff_cache", "__pycache__", ".claude"}
    hits = []
    for path in ROOT.rglob("*"):
        if (path.is_file() and not skip & set(path.relative_to(ROOT).parts) and path.name != ".env"
                and path.suffix in {".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".csv", ".json", ".example", ""}):
            text = path.read_text(encoding="utf-8", errors="ignore")
            hits += [f"{path.relative_to(ROOT)}" for _ in key_like.finditer(text)]
    assert not hits, f"possible API keys in: {sorted(set(hits))}"
