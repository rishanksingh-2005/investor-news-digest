"""Watchlist import: Excel, CSV/TSV, JSON or a plain list, with auto-detected columns."""

from __future__ import annotations

import os
import re
import csv
import io
import json
import openpyxl

from . import models, patterns, publishing, settings


# ============================================================================
# Watchlist loader: Excel, CSV/TSV, JSON or a plain list of names.
#
# Any customer's own spreadsheet works as long as it has a column with the
# company name; every other column is optional and recognised by common
# header names (see WATCHLIST_COLUMNS) or mapped explicitly with --map.
# ============================================================================
class WatchlistError(ValueError):
    pass


# canonical field -> accepted header names (compared lower-case, with spaces,
# underscores, dots and dashes removed)
WATCHLIST_COLUMNS = {
    "name": ["name", "company", "companyname", "company name", "issuer", "issuername", "securityname",
             "security", "entity", "entityname", "organisation", "organization", "longname", "shortname"],
    "ticker": ["ticker", "symbol", "tickersymbol", "microsoftexcelticker", "bloombergticker", "epic", "code"],
    "exchange": ["exchange", "mic", "exchangemic", "market", "listing", "venue", "primaryexchange"],
    "currency": ["currency", "ccy", "tradingcurrency"],
    "country": ["country", "countryofrisk", "domicile", "region", "countrycode"],
    "tier": ["tier", "priority", "group", "bucket", "level", "rank"],
    "aliases": ["aliases", "alias", "alsoknownas", "aka", "othernames", "alternativenames", "tradingnames"],
    "exclude": ["exclude", "excludeterms", "exclusions", "negativekeywords", "notabout", "ignoreterms"],
}


def _split_terms(v) -> list[str]:
    """'Jet2; Jet2.com | Jet2 plc' -> ['Jet2', 'Jet2.com', 'Jet2 plc']. Only ; and |
    separate terms, because company names themselves contain commas."""
    if isinstance(v, (list, tuple)):
        items = [str(x) for x in v]
    else:
        items = re.split(r"[;|\n]", _cell(v))
    return list(dict.fromkeys(t.strip() for t in items if t and t.strip()))

# Exchange names people actually type -> MIC.
EXCHANGE_ALIASES = {
    "NASDAQ": "XNAS", "NASDAQGS": "XNAS", "NASDAQGM": "XNAS", "NASDAQCM": "XNAS", "NMS": "XNAS",
    "NYSE": "XNYS", "NEWYORKSTOCKEXCHANGE": "XNYS", "NYSEAMERICAN": "XASE", "AMEX": "XASE",
    "NYSEARCA": "ARCX", "CBOE": "BATS", "OTC": "OTCM", "OTCQX": "OTCM", "OTCQB": "OTCM", "PINK": "OTCM",
    "LSE": "XLON", "LON": "XLON", "LONDON": "XLON", "AIM": "XLON", "LONDONSTOCKEXCHANGE": "XLON",
    "TSX": "XTSE", "TORONTO": "XTSE", "TSXV": "XTSX", "TSXVENTURE": "XTSX",
    "ASX": "XASX", "EURONEXTPARIS": "XPAR", "EPA": "XPAR", "PARIS": "XPAR",
    "EURONEXTAMSTERDAM": "XAMS", "AMS": "XAMS", "EURONEXTLISBON": "XLIS",
    "STOCKHOLM": "XSTO", "NASDAQSTOCKHOLM": "XSTO", "BORSAITALIANA": "XMIL", "MILAN": "XMIL",
    "TASE": "XTAE", "TELAVIV": "XTAE", "XETRA": "XFRA", "FRANKFURT": "XFRA", "FRA": "XFRA",
    "SIX": "XSWX", "SWX": "XSWX", "WSE": "XWAR", "GPW": "XWAR", "WARSAW": "XWAR",
}

COUNTRY_ALIASES = {
    "US": "US", "USA": "US", "UNITEDSTATES": "US", "UNITEDSTATESOFAMERICA": "US",
    "UK": "UK", "GB": "UK", "GBR": "UK", "UNITEDKINGDOM": "UK", "GREATBRITAIN": "UK", "ENGLAND": "UK",
    "CA": "CA", "CAN": "CA", "CANADA": "CA",
}

# Values spreadsheets put in cells that mean "nothing here".
_EMPTY_CELL = {"", "#VALUE!", "#N/A", "#NAME?", "#REF!", "#DIV/0!", "N/A", "NA", "NONE", "NULL", "-"}


def _norm_header(h) -> str:
    return re.sub(r"[\s_.\-/()]+", "", str(h or "")).lower()


def _cell(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.upper() in _EMPTY_CELL else s


def normalise_exchange(v) -> str:
    s = _cell(v)
    key = re.sub(r"[\s_.\-:]+", "", s).upper()
    if not key:
        return ""
    return EXCHANGE_ALIASES.get(key, key if re.fullmatch(r"[A-Z]{4}", key) else s)


def normalise_country(v) -> str:
    key = re.sub(r"[\s_.\-]+", "", _cell(v)).upper()
    return COUNTRY_ALIASES.get(key, key)


def parse_tier(v):
    """1, 1.0, "1", "Tier 1", "T1", "P2" -> int; anything else -> None."""
    s = _cell(v)
    if not s:
        return None
    m = re.fullmatch(r"(?:tier|t|priority|p|group|g)?\s*(\d+)(?:\.0+)?", s, re.I)
    return int(m.group(1)) if m else None


def parse_column_map(spec: str) -> dict:
    """'name=Issuer, tier=Priority' -> {'name': 'Issuer', 'tier': 'Priority'}."""
    out = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise WatchlistError(f"--map entry {part.strip()!r} must look like field=Column Header")
        k, v = (x.strip() for x in part.split("=", 1))
        if k.lower() not in WATCHLIST_COLUMNS:
            raise WatchlistError(f"--map field {k!r} unknown; use one of {', '.join(WATCHLIST_COLUMNS)}")
        out[k.lower()] = v
    return out


def _match_columns(header: list, column_map: dict) -> dict:
    """Header cells -> {field: column index}. Explicit --map entries win."""
    normed = [_norm_header(h) for h in header]
    found = {}
    for fld, aliases in WATCHLIST_COLUMNS.items():
        if fld in column_map:
            want = _norm_header(column_map[fld])
            if want not in normed:
                raise WatchlistError(f"column {column_map[fld]!r} (mapped to {fld}) not found; "
                                     f"columns are: {', '.join(str(h) for h in header if h)}")
            found[fld] = normed.index(want)
            continue
        for alias in aliases:
            a = _norm_header(alias)
            if a in normed and normed.index(a) not in found.values():
                found[fld] = normed.index(a)
                break
    return found


def _find_header(rows: list, column_map: dict):
    """The header is the first of the top 15 rows that has a recognisable name column."""
    error = None
    for i, row in enumerate(rows[:15]):
        if not any(_cell(c) for c in row):
            continue
        try:
            cols = _match_columns(list(row), column_map)
        except WatchlistError as e:   # a --map column missing from this row
            error = error or e
            continue
        if "name" in cols:
            return i, cols
    if error:
        raise error
    return None, {}


def _read_rows(path: str, sheet: str | None) -> list[list]:
    """Raw rows from any supported file, header row included where there is one."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        try:
            if sheet:
                if sheet not in wb.sheetnames:
                    raise WatchlistError(f"sheet {sheet!r} not found; sheets are: {', '.join(wb.sheetnames)}")
                sheets = [wb[sheet]]
            else:   # first sheet that has a name column, active sheet first
                sheets = [wb.active] + [ws for ws in wb.worksheets if ws is not wb.active]
            for ws in sheets:
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                if len(sheets) == 1 or _find_header(rows, {})[0] is not None:
                    return rows
            return [list(r) for r in sheets[0].iter_rows(values_only=True)]
        finally:
            wb.close()
    if ext == ".xls":
        raise WatchlistError("old .xls files aren't supported: open it in Excel and Save As .xlsx or .csv")
    if ext == ".json":
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("companies") or data.get("watchlist") or []
        if not isinstance(data, list):
            raise WatchlistError("JSON watchlist must be a list, or an object with a 'companies' list")
        if all(isinstance(x, str) for x in data):
            return [["name"]] + [[x] for x in data]
        keys = list(dict.fromkeys(k for x in data if isinstance(x, dict) for k in x))
        return [keys] + [[x.get(k) for k in keys] for x in data if isinstance(x, dict)]
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    if not text.strip():
        return []
    if ext == ".txt":   # one company per line; '#' starts a comment
        names = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
        return [["name"]] + [[n] for n in names if n]
    # CSV / TSV: sniff the delimiter; a single column with no header is a list of names.
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if ext == ".tsv" else csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect)]
    if rows and all(len(r) <= 1 for r in rows) and _find_header(rows, {})[0] is None:
        rows = [["name"]] + rows
    return rows


def default_watchlist() -> str:
    """WATCHLIST env var, else the first watchlist file next to the script."""
    env = os.getenv("WATCHLIST", "").strip()
    if env:
        return env
    for fn in ("watchlist.xlsx", "watchlist.csv", "watchlist.json", "watchlist.txt", "Company List.xlsx"):
        if os.path.exists(os.path.join(settings.HERE, fn)):
            return os.path.join(settings.HERE, fn)
    return os.path.join(settings.HERE, "examples", "watchlist.example.csv")


def read_watchlist(cfg: settings.Config) -> tuple[list[models.Company], dict]:
    """Every company in the watchlist (all tiers) plus a report of what was detected."""
    path = cfg.watchlist
    if not os.path.exists(path):
        raise WatchlistError(f"watchlist not found: {path}")
    rows = _read_rows(path, cfg.sheet_name)
    column_map = parse_column_map(cfg.column_map) if isinstance(cfg.column_map, str) else dict(cfg.column_map)
    h, cols = _find_header(rows, column_map)
    if h is None:
        first = next((r for r in rows if any(_cell(c) for c in r)), [])
        raise WatchlistError(
            "couldn't find a company-name column. Name a column 'Company' or 'Name', or map yours with "
            f"--map name=\"Your Header\". First row is: {', '.join(str(c) for c in first if c is not None)}")
    report = {"columns": {f: str(rows[h][i]) for f, i in cols.items()}, "skipped": [], "duplicates": 0}

    def get(r, fld):
        i = cols.get(fld)
        return r[i] if i is not None and i < len(r) else None

    out, seen = [], set()
    for n, r in enumerate(rows[h + 1:], start=h + 2):
        name = _cell(get(r, "name"))
        if not name:
            continue
        if "tier" in cols:
            tier = parse_tier(get(r, "tier"))
            if tier is None:
                raw = _cell(get(r, "tier"))
                report["skipped"].append(f"row {n} ({name}): " + (f"tier {raw!r} is not a number"
                                                                   if raw else "no tier"))
                continue
        else:
            tier = cfg.default_tier
        if name.lower() in seen:
            report["duplicates"] += 1
            continue
        seen.add(name.lower())
        out.append(models.Company(
            name=name, tier=tier,
            currency=_cell(get(r, "currency")),
            exchange=normalise_exchange(get(r, "exchange")),
            ticker=_cell(get(r, "ticker")),
            country=normalise_country(get(r, "country")),
            aliases=[a for a in _split_terms(get(r, "aliases")) if a.lower() != name.lower()],
            exclude=_split_terms(get(r, "exclude")),
        ))
    return out, report


def load_companies(cfg: settings.Config) -> list[models.Company]:
    if cfg.companies:   # --company "Name" on the command line: no file needed
        return [models.Company(name=n.strip(), tier=cfg.default_tier) for n in cfg.companies if n.strip()]
    companies, report = read_watchlist(cfg)
    for msg in report["skipped"][:5]:
        print(f"   skipped {msg}")
    if len(report["skipped"]) > 5:
        print(f"   ... and {len(report['skipped']) - 5} more rows without a valid tier")
    return [c for c in companies if c.tier in cfg.tiers]


def check_watchlist(cfg: settings.Config) -> int:
    """--check: show how the watchlist was read, without fetching anything."""
    try:
        companies, report = read_watchlist(cfg)
    except (WatchlistError, OSError, json.JSONDecodeError) as e:
        print(f"Watchlist problem: {e}")
        return 1
    print(f"Watchlist: {cfg.watchlist}")
    for fld in WATCHLIST_COLUMNS:
        src = report["columns"].get(fld)
        print(f"  {fld:<9}<- {src!r}" if src else
              f"  {fld:<9}   (not found{'; every company is tier ' + str(cfg.default_tier) if fld == 'tier' else ''})")
    tiers: dict = {}
    for c in companies:
        tiers.setdefault(c.tier, []).append(c)
    print(f"\n{len(companies)} companies")
    for t in sorted(tiers):
        names = [c.name for c in tiers[t]]
        more = f" ... +{len(names) - 6} more" if len(names) > 6 else ""
        print(f"  Tier {t}: {len(names):>4}  {', '.join(names[:6])}{more}")
    unknown = sorted({c.exchange for c in companies if c.exchange and c.exchange.upper() not in
                      patterns.EXCHANGE_SOURCES and c.exchange.upper() not in patterns.NORTH_AM_EXCHANGES})
    if unknown:
        print(f"\n  Exchanges without a dedicated source (generic wires used): {', '.join(unknown)}")
    if report["duplicates"]:
        print(f"  {report['duplicates']} duplicate names ignored")
    for msg in report["skipped"]:
        print(f"  skipped {msg}")
    print(f"\nSelected tiers {publishing._tiers_label(cfg)}: {sum(c.tier in cfg.tiers for c in companies)} companies")
    return 0


WATCHLIST_TEMPLATE = [
    ["Company", "Ticker", "Exchange", "Country", "Currency", "Tier", "Aliases", "Exclude"],
    ["Apple Inc.", "AAPL", "XNAS", "US", "USD", 1, "Apple", "Apple Records; Big Apple"],
    ["Unilever PLC", "ULVR", "XLON", "UK", "GBp", 2, "", ""],
    ["Shopify Inc.", "SHOP", "XTSE", "CA", "CAD", 3, "", ""],
]


def write_watchlist_template(path: str) -> None:
    if os.path.exists(path):
        raise WatchlistError(f"{path} already exists; not overwriting it")
    if path.lower().endswith((".xlsx", ".xlsm")):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Watchlist"
        for row in WATCHLIST_TEMPLATE:
            ws.append(row)
        wb.save(path)
    elif path.lower().endswith(".json"):
        keys = [k.lower() for k in WATCHLIST_TEMPLATE[0]]
        with open(path, "w", encoding="utf-8") as f:
            json.dump([dict(zip(keys, r)) for r in WATCHLIST_TEMPLATE[1:]], f, indent=2)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(WATCHLIST_TEMPLATE)
