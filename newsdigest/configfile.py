"""config.yaml: every setting in a file, so nobody has to edit Python.

    newsdigest --init-config            # writes config.yaml with every setting explained
    newsdigest --config client-a.yaml   # use a specific file

Where the file comes from: --config FILE, else $NEWS_DIGEST_CONFIG, else
config.yaml / config.yml in the project folder. Keys are the setting names
(they may be grouped under any section heading you like). Unknown keys are an
error, with a suggestion, so a typo never silently does nothing. Paths are
relative to the config file. Command-line flags override the file.
"""

from __future__ import annotations

import dataclasses
import difflib
import inspect
import os
import re

import yaml

from . import settings, watchlists

# Settings that are command-line actions, not configuration.
RUNTIME_ONLY = {"companies", "check_only", "init_watchlist", "doctor", "config_path", "init_config",
                "schedule", "unschedule"}
PATH_FIELDS = {"watchlist", "output_dir", "cache_path", "log_dir", "bart_model_dir"}
_TRUE = {"true", "yes", "on", "1", "y"}
_FALSE = {"false", "no", "off", "0", "n"}


class ConfigError(ValueError):
    pass


def find_config(explicit: str | None = None) -> str | None:
    if explicit:
        path = os.path.abspath(os.path.expanduser(explicit))
        if not os.path.exists(path):
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get("NEWS_DIGEST_CONFIG", "").strip()
    if env:
        path = os.path.abspath(os.path.expanduser(env))
        if not os.path.exists(path):
            raise ConfigError(f"NEWS_DIGEST_CONFIG points to a missing file: {path}")
        return path
    for fn in ("config.yaml", "config.yml"):
        path = os.path.join(settings.HERE, fn)
        if os.path.exists(path):
            return path
    return None


def _settable_fields(cfg) -> dict:
    return {f.name: f for f in dataclasses.fields(cfg) if f.name not in RUNTIME_ONLY}


def _flatten(data: dict, fields: dict, path: str) -> list[tuple[str, object]]:
    """Top-level keys, plus the keys of any section ("sources:", "llm:", ...)."""
    out = []
    for key, value in data.items():
        k = _norm_key(key)
        if k not in fields and isinstance(value, dict):
            for sk, sv in value.items():
                if isinstance(sv, dict) and _norm_key(sk) not in fields:
                    raise ConfigError(f"{path}: sections can only be one level deep ({key} -> {sk})")
                out.append((str(sk), sv))
        else:
            out.append((str(key), value))
    return out


def _norm_key(key) -> str:
    return re.sub(r"[\s-]+", "_", str(key).strip()).lower()


def _coerce(name: str, value, default):
    def bad(want):
        return ConfigError(f"setting {name!r} must be {want}, got {value!r}")

    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise bad("true or false")
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise bad("a whole number")
        try:
            return int(value)
        except ValueError:
            raise bad("a whole number") from None
    if isinstance(default, float):
        if isinstance(value, bool):
            raise bad("a number")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise bad("a number") from None
    if isinstance(default, tuple):            # tiers
        items = value if isinstance(value, (list, tuple)) else str(value).split(",")
        try:
            return tuple(int(str(x).strip()) for x in items if str(x).strip())
        except ValueError:
            raise bad("a list of whole numbers, e.g. [1, 2]") from None
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise bad("a mapping (key: value lines)")
        return dict(value)
    if isinstance(default, list):
        if not isinstance(value, list):
            raise bad("a list")
        return list(value)
    # str, or str | None
    if value is None:
        if default is None:
            return None
        raise bad("a value (or remove the line to use the default)")
    if isinstance(value, (dict, list)):
        raise bad("text")
    return str(value)


def load_config(path: str, cfg) -> list[str]:
    """Apply a config file to cfg. Returns the setting names it set."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected 'setting: value' lines at the top level")
    fields = _settable_fields(cfg)
    base = os.path.dirname(os.path.abspath(path))
    applied = []
    for key, value in _flatten(data, fields, path):
        name = _norm_key(key)
        if name not in fields:
            hint = difflib.get_close_matches(name, list(fields), n=1)
            raise ConfigError(f"{path}: unknown setting {key!r}" + (f" (did you mean {hint[0]!r}?)" if hint else "")
                              + f". See `{settings.command()} --init-config` for the full list.")
        try:
            v = _coerce(name, value, getattr(cfg, name))
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}") from None
        if name in PATH_FIELDS and isinstance(v, str) and v:
            v = os.path.normpath(os.path.join(base, os.path.expanduser(v)))
        if name == "column_map":
            v = watchlists.parse_column_map(",".join(f"{k}={x}" for k, x in v.items()))
        if name == "llm_provider" and v.lower() not in ("ollama", "openai", "anthropic", "bart"):
            raise ConfigError(f"{path}: llm_provider must be ollama, openai, anthropic or bart")
        setattr(cfg, name, v)
        applied.append(name)
    cfg.config_path = path
    return applied


# --- starter file ---------------------------------------------------------------------

_ACTIVE = {"watchlist", "tiers", "days_back", "llm_provider"}


def _yaml_value(v) -> str:
    if isinstance(v, tuple):
        v = list(v)
    s = yaml.safe_dump(v, default_flow_style=True, allow_unicode=True, width=1000).strip()
    return s[:-4].strip() if s.endswith("\n...") else s


def _display_default(name: str, value):
    """Defaults shown relative to the project folder, the way you'd write them."""
    if name in PATH_FIELDS and isinstance(value, str) and value:
        try:
            rel = os.path.relpath(value, settings.HERE)
        except ValueError:      # different drive on Windows
            return value
        return "." if rel == "." else rel.replace("\\", "/")
    return value


def starter_config() -> str:
    """config.yaml text listing every setting with its explanation, generated
    from the Config class itself so it can never drift out of date."""
    cfg = settings.Config()
    fields = _settable_fields(cfg)
    src = inspect.getsource(settings.Config).splitlines()
    out = [
        "# News digest settings. Uncomment a line (remove the leading '# ') to change it.",
        "# Any setting not listed here keeps its default. Command-line flags override this file.",
        "# API keys don't go here: they live in .env (see .env.example).",
        "",
    ]
    pending: list[str] = []
    for line in src[1:]:
        s = line.strip()
        if not s:
            if pending:
                out += pending
                pending = []
            continue
        if s.startswith("#"):
            text = s.lstrip("# ").rstrip()
            if len(line) - len(line.lstrip()) > 20 and out and not pending:
                out.append(f"#{' ' * 30}{text}")     # continuation of the previous setting's comment
            else:
                pending.append("# " + text)
            continue
        m = re.match(r"(\w+)\s*:", s)
        if not m or m.group(1) not in fields:
            pending = []
            continue
        name = m.group(1)
        comment = s.split("#", 1)[1].strip() if "#" in s and not s.split("#", 1)[0].count('"') % 2 else ""
        if pending:
            if out and out[-1] != "":
                out.append("")
            out += pending
            pending = []
        value = _display_default(name, getattr(cfg, name))
        if name == "watchlist" and not os.path.exists(getattr(cfg, name)):
            value = "watchlist.xlsx"
        entry = f"{name}: {_yaml_value(value)}"
        if comment:
            entry += f"    # {comment}"
        out.append(entry if name in _ACTIVE else "# " + entry)
    return "\n".join(out).rstrip() + "\n"


def write_starter_config(path: str) -> None:
    if os.path.exists(path):
        raise ConfigError(f"{path} already exists; not overwriting it")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(starter_config())
