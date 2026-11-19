"""Score the digest's rule-based judgement against hand-labelled cases.

    python eval/evaluate.py            # scores + every miss
    python eval/evaluate.py --quiet    # scores only

Three things are measured, all offline (no network, no LLM):

  relevance   would a story be kept for this company? (pre-filter + final gate)
  figures     does the number check flag a bullet's figures as not in the source?
  duplicates  are two stories treated as the same event?

Exit code 1 if any score is below its threshold in golden.json, so CI catches a
change that makes the rules worse. Add a case whenever you find a real mistake.
"""
from __future__ import annotations

import argparse
import types
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden.json"


def load_digest():
    """The parts of the newsdigest package the scorer calls, as one namespace."""
    sys.path.insert(0, str(HERE.parent))
    from newsdigest import filtering, grouping, models, settings, summarising
    return types.SimpleNamespace(
        Config=settings.Config, Article=models.Article, Company=models.Company,
        filter_articles=filtering.filter_articles, is_low_quality=filtering.is_low_quality,
        is_material=filtering.is_material, strongly_relevant=filtering.strongly_relevant,
        is_excluded=filtering.is_excluded, _same_event=grouping._same_event,
        unsupported_figures=summarising.unsupported_figures)


def _company(nd, spec: dict):
    return nd.Company(name=spec["name"], ticker=spec.get("ticker", ""), exchange=spec.get("exchange", ""),
                      aliases=list(spec.get("aliases", [])), exclude=list(spec.get("exclude", [])))


def would_keep(nd, cfg, case: dict, company) -> bool:
    """The same gates a story passes in a real run, with headline + snippet
    standing in for the AI bullet (so this measures the rules, not the model)."""
    a = nd.Article(headline=case["headline"], url="https://example.com/x", snippet=case.get("snippet", ""))
    if not nd.filter_articles([a], company, cfg):
        return False
    a.bullet = f"{a.headline}. {a.snippet}".strip(". ")
    return (not nd.is_low_quality(a.bullet) and nd.is_material(a.bullet, cfg)
            and nd.strongly_relevant(a, company) and not nd.is_excluded(a.bullet, company))


def same_event(nd, cfg, case: dict, company) -> bool:
    a = nd.Article(headline=case["a"]["headline"], url="https://example.com/a", bullet=case["a"]["bullet"])
    b = nd.Article(headline=case["b"]["headline"], url="https://example.com/b", bullet=case["b"]["bullet"])
    return nd._same_event(a, b, company, cfg)


def _scores(results: list[tuple[bool, bool]]) -> dict:
    """results: (expected, predicted) with True = the positive class."""
    tp = sum(1 for e, p in results if e and p)
    fp = sum(1 for e, p in results if not e and p)
    fn = sum(1 for e, p in results if e and not p)
    tn = sum(1 for e, p in results if not e and not p)
    n = len(results) or 1
    return {"n": len(results), "accuracy": (tp + tn) / n,
            "precision": tp / (tp + fp) if tp + fp else 1.0,
            "recall": tp / (tp + fn) if tp + fn else 1.0}


def evaluate(nd=None, golden: dict | None = None) -> dict:
    nd = nd or load_digest()
    golden = golden or json.loads(GOLDEN.read_text(encoding="utf-8"))
    cfg = nd.Config()
    cfg.days_back = cfg.recency_grace_days = 36500   # cases carry no dates
    companies = {k: _company(nd, v) for k, v in golden["companies"].items()}
    out = {}

    rows = []
    for c in golden["relevance"]:
        got = would_keep(nd, cfg, c, companies[c["company"]])
        rows.append((c["keep"], got, f"[{c['company']}] {c['headline']}", c.get("note", "")))
    out["relevance"] = (_scores([(e, p) for e, p, *_ in rows]), rows, "kept")

    rows = []
    for c in golden["figures"]:
        got = bool(nd.unsupported_figures(c["bullet"], c["source"]))
        rows.append((c["flagged"], got, f"{c['bullet']}  <-  {c['source']}", c.get("note", "")))
    out["figures"] = (_scores([(e, p) for e, p, *_ in rows]), rows, "flagged")

    rows = []
    for c in golden["duplicates"]:
        got = same_event(nd, cfg, c, companies[c["company"]])
        rows.append((c["same"], got, f"{c['a']['bullet']}  |  {c['b']['bullet']}", c.get("note", "")))
    out["duplicates"] = (_scores([(e, p) for e, p, *_ in rows]), rows, "same")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    results = evaluate(golden=golden)
    failed = False
    print(f"{'':<12} {'cases':>5} {'accuracy':>9} {'precision':>10} {'recall':>7}   threshold")
    for name, (s, rows, label) in results.items():
        th = golden["thresholds"][name]
        mark = "ok" if s["accuracy"] >= th else "BELOW"
        failed |= s["accuracy"] < th
        print(f"{name:<12} {s['n']:>5} {s['accuracy']:>9.0%} {s['precision']:>10.0%} {s['recall']:>7.0%}"
              f"   {th:.0%} {mark}")
    if not args.quiet:
        for name, (_, rows, label) in results.items():
            misses = [r for r in rows if r[0] != r[1]]
            if misses:
                print(f"\n{name}: {len(misses)} wrong")
                for expected, got, text, note in misses:
                    print(f"  expected {label}={expected}, got {got}: {text}")
                    if note:
                        print(f"      note: {note}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
