"""Evaluation of the rule-based evidence extractor (offline, deterministic).

    python evaluation/run_evidence_eval.py           # print the report
    python evaluation/run_evidence_eval.py --write   # also write evaluation/EVIDENCE_RESULTS.md

Three things are measured:
  1. ACCURACY against `evidence_gold.json` (10 hand-read papers), field by field.
       correct      the extractor's value matches what the abstract says
       correct-null the abstract does not say it, and the extractor correctly said "unavailable"
       missed       the abstract says it, the extractor said "unavailable"   (lost information)
       wrong        both have a value, but they disagree                     (bad)
       fabricated   the abstract does NOT say it, the extractor produced a value  (worst)
  2. TRACEABILITY of all ~390 snapshot papers: every value must really occur in the text it cites.
  3. AVAILABILITY and LATENCY.
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # make `app` importable

from app.schemas.evidence import Evidence, FieldStatus  # noqa: E402
from app.schemas.paper import Paper  # noqa: E402
from app.services.deduplicator import deduplicate  # noqa: E402
from app.services.evidence.rule_based import RuleBasedEvidenceExtractor, _numbers_as_digits  # noqa: E402

FIELDS = ["study_design", "participants", "studies", "population", "intervention", "comparator", "outcomes", "limitations_stated"]
TEXT_FIELDS = ["population", "intervention", "comparator", "outcomes"]


def _squash(text: str) -> str:
    return "".join(text.split()).lower()


def load_papers() -> dict[str, Paper]:
    """Every unique paper in the snapshot (the same set the retrieval evaluation uses)."""
    pools = json.loads((HERE / "pools.json").read_text(encoding="utf-8"))
    papers: dict[str, Paper] = {}
    for entry in pools["questions"].values():
        raw = [Paper(**d) for provider in ("pubmed", "crossref") for d in entry["providers"][provider]]
        for paper in deduplicate(raw):
            papers[paper.id] = paper
    return papers


def extracted_text(evidence: Evidence, field: str) -> str | None:
    """What the extractor says for a text-like field, flattened to one string (None = unavailable)."""
    value = getattr(evidence, field)
    if value.status is FieldStatus.UNAVAILABLE:
        return None
    return " | ".join(value.values) if field == "outcomes" else value.value


def judge(expected, got, is_text_field: bool) -> str:
    """Classify one field: correct / correct-null / missed / wrong / fabricated."""
    if expected is None:
        return "correct-null" if got is None else "fabricated"
    if got is None:
        return "missed"
    if is_text_field:
        ok = all(keyword.lower() in got.lower() for keyword in expected)
    else:
        ok = expected == got
    return "correct" if ok else "wrong"


def score_paper(gold: dict, evidence: Evidence) -> dict[str, str]:
    results = {
        "study_design": judge(gold["study_design"], evidence.study_design.value, False),
        "participants": judge(gold["participants"], evidence.sample_size.participants, False),
        "studies": judge(gold["studies"], evidence.sample_size.studies, False),
        "limitations_stated": "correct" if (evidence.limitations.status is not FieldStatus.UNAVAILABLE) == gold["limitations_stated"]
        else ("fabricated" if not gold["limitations_stated"] else "missed"),
    }
    for field in TEXT_FIELDS:
        results[field] = judge(gold[field], extracted_text(evidence, field), True)
    if gold["limitations_stated"] is False and results["limitations_stated"] == "correct":
        results["limitations_stated"] = "correct-null"
    return results


def statistics_recall(gold: dict, evidence: Evidence) -> tuple[int, int]:
    """(expected statistics found, expected statistics)."""
    found = {_squash(stat) for finding in evidence.findings.items for stat in finding.statistics}
    expected = [_squash(stat) for stat in gold["statistics"]]
    return sum(stat in found for stat in expected), len(expected)


def audit_traceability(papers: dict[str, Paper], extractor: RuleBasedEvidenceExtractor) -> dict:
    """Check that no value is invented: each must literally occur in the text it cites."""
    violations: list[str] = []
    total_values = 0
    for paper in papers.values():
        ev = extractor.extract_sync(paper)
        source = {"abstract": paper.abstract or "", "title": paper.title}

        def check(span, claim: str) -> None:
            if source[span.origin][span.start : span.end] != span.text:
                violations.append(f"{paper.id}: span does not match the paper text ({claim})")

        for field_name in ("study_design", "population", "sample_size", "intervention", "comparator", "outcomes", "limitations"):
            field = getattr(ev, field_name)
            for span in field.sources:
                check(span, field_name)
            if field.status is FieldStatus.UNAVAILABLE:
                continue
            cited = " ".join(span.text for span in field.sources)
            if field_name in ("population", "intervention", "comparator"):
                total_values += 1
                if _squash(field.value) not in _squash(cited):
                    violations.append(f"{paper.id}: {field_name} value not in its cited text: {field.value!r}")
            elif field_name in ("outcomes", "limitations"):
                for value in field.values:
                    total_values += 1
                    if _squash(value) not in _squash(cited):
                        violations.append(f"{paper.id}: {field_name} item not in its cited text: {value!r}")
            elif field_name == "sample_size":
                for number in (field.participants, field.studies):
                    if number is not None:
                        total_values += 1
                        # "Seventy-five adults" cites 75: compare against the cited text with number words turned into digits
                        digits = {_squash(str(number)), _squash(f"{number:,}")}
                        if not any(d in _squash(_numbers_as_digits(cited)) for d in digits):
                            violations.append(f"{paper.id}: sample size {number} not in its cited text")
        for finding in ev.findings.items:
            check(finding.source, "finding")
            for stat in finding.statistics:
                total_values += 1
                if _squash(stat) not in _squash(finding.source.text):
                    violations.append(f"{paper.id}: statistic {stat!r} not in its sentence")
        if ev.paper_id != paper.id:
            violations.append(f"{paper.id}: evidence is linked to the wrong paper ({ev.paper_id})")
    return {"papers": len(papers), "values_checked": total_values, "violations": violations}


def availability(papers: dict[str, Paper], extractor: RuleBasedEvidenceExtractor) -> dict:
    names = ("study_design", "population", "sample_size", "intervention", "comparator", "outcomes", "findings", "limitations")
    with_abstract = [p for p in papers.values() if p.abstract]
    counts = {name: Counter() for name in names}
    for paper in with_abstract:
        evidence = extractor.extract_sync(paper)  # once per paper, not once per field
        for name in names:
            counts[name][getattr(evidence, name).status.value] += 1
    rows = {
        name: {"stated": c["stated"], "inferred": c["inferred"], "unavailable": c["unavailable"], "total": len(with_abstract)}
        for name, c in counts.items()
    }
    return {"papers": len(papers), "with_abstract": len(with_abstract), "fields": rows}


def latency_ms(papers: list[Paper], extractor: RuleBasedEvidenceExtractor, repeats: int = 30) -> dict:
    """Per-paper extraction time. Best of `repeats` full passes (to damp timer noise), plus a spread."""
    per_pass = []
    for _ in range(repeats):
        start = time.perf_counter()
        for paper in papers:
            extractor.extract_sync(paper)
        per_pass.append((time.perf_counter() - start) / len(papers) * 1000)
    per_paper = []
    for paper in papers:
        start = time.perf_counter()
        extractor.extract_sync(paper)
        per_paper.append((time.perf_counter() - start) * 1000)
    per_paper.sort()
    return {
        "mean_ms_per_paper": min(per_pass),
        "median_ms": statistics.median(per_paper),
        "p95_ms": per_paper[int(0.95 * (len(per_paper) - 1))],
        "max_ms": per_paper[-1],
    }


def evaluate(gold_file: str = "evidence_gold.json", latency: bool = True) -> dict:
    """`evidence_gold.json` = the development set; `evidence_holdout.json` = papers never used to tune the rules."""
    extractor = RuleBasedEvidenceExtractor()
    papers = load_papers()
    gold = json.loads((HERE / gold_file).read_text(encoding="utf-8"))["papers"]
    missing = [g["id"] for g in gold if g["id"] not in papers]
    assert not missing, f"gold papers not in the snapshot: {missing}"

    per_field: dict[str, Counter] = {f: Counter() for f in FIELDS}
    problems: list[dict] = []
    found_stats = total_stats = 0
    for g in gold:
        evidence = extractor.extract_sync(papers[g["id"]])
        scored = score_paper(g, evidence)
        for field, outcome in scored.items():
            per_field[field][outcome] += 1
            if outcome in ("wrong", "missed", "fabricated"):
                got = {
                    "study_design": evidence.study_design.value,
                    "participants": evidence.sample_size.participants,
                    "studies": evidence.sample_size.studies,
                    "limitations_stated": evidence.limitations.status.value,
                }.get(field) if field not in TEXT_FIELDS else extracted_text(evidence, field)
                expected = g[field]
                problems.append({"id": g["id"], "field": field, "outcome": outcome, "expected": expected, "got": got, "note": g["notes"]})
        hit, expect = statistics_recall(g, evidence)
        found_stats += hit
        total_stats += expect

    return {
        "gold_papers": len(gold),
        "per_field": {f: dict(c) for f, c in per_field.items()},
        "statistics": {"found": found_stats, "expected": total_stats},
        "problems": problems,
        "traceability": audit_traceability(papers, extractor),
        "availability": availability(papers, extractor),
        # timing is slow to measure (many repeated passes), so tests switch it off with latency=False
        "latency_gold_set": latency_ms([papers[g["id"]] for g in gold], extractor) if latency else None,
        "latency_all_snapshot": latency_ms(list(papers.values()), extractor, repeats=5) if latency else None,
    }


def render(result: dict) -> str:
    lines = [f"Gold set: {result['gold_papers']} hand-read papers", "", "| Field | correct | correct-null | missed | wrong | fabricated | accuracy |", "|---|--:|--:|--:|--:|--:|--:|"]
    total_ok = total = 0
    for field, counts in result["per_field"].items():
        n = sum(counts.values())
        ok = counts.get("correct", 0) + counts.get("correct-null", 0)
        total_ok, total = total_ok + ok, total + n
        lines.append(
            f"| {field} | {counts.get('correct', 0)} | {counts.get('correct-null', 0)} | {counts.get('missed', 0)} | "
            f"{counts.get('wrong', 0)} | {counts.get('fabricated', 0)} | {ok}/{n} ({ok / n:.0%}) |"
        )
    stats = result["statistics"]
    lines.append(f"| statistics (recall) | {stats['found']}/{stats['expected']} found | | | | | {stats['found'] / max(stats['expected'], 1):.0%} |")
    lines.append(f"| **all fields** | | | | | | **{total_ok}/{total} ({total_ok / total:.0%})** |")

    lines += ["", "### Mistakes and misses (with the reviewer's note)", ""]
    for p in result["problems"]:
        lines.append(f"* `{p['id']}` **{p['field']}** is {p['outcome']}: expected {p['expected']!r}, got {p['got']!r}. _{p['note']}_")

    t = result["traceability"]
    lines += ["", f"### Traceability audit (all {t['papers']} snapshot papers, {t['values_checked']} extracted values)",
              f"Values that do NOT occur in the text they cite: **{len(t['violations'])}**"]
    lines += [f"* {v}" for v in t["violations"][:10]]

    a = result["availability"]
    lines += ["", f"### How often each field is unavailable ({a['with_abstract']} snapshot papers that have an abstract)", "",
              "| Field | stated | inferred (title) | unavailable |", "|---|--:|--:|--:|"]
    for field, c in a["fields"].items():
        lines.append(f"| {field} | {c['stated']} ({c['stated'] / c['total']:.0%}) | {c['inferred']} | {c['unavailable']} ({c['unavailable'] / c['total']:.0%}) |")

    if result["latency_gold_set"] is None:
        return "\n".join(lines)
    g, s = result["latency_gold_set"], result["latency_all_snapshot"]
    lines += ["", "### Latency (pure extraction, no network)",
              f"* gold set: mean {g['mean_ms_per_paper']:.2f} ms/paper, median {g['median_ms']:.2f}, p95 {g['p95_ms']:.2f}, max {g['max_ms']:.2f} ms",
              f"* all {a['papers']} snapshot papers: mean {s['mean_ms_per_paper']:.2f} ms/paper, median {s['median_ms']:.2f}, p95 {s['p95_ms']:.2f}, max {s['max_ms']:.2f} ms"]
    return "\n".join(lines)


HISTORY = """## How to read these numbers

| Set | Papers | When measured | Result | Can it be trusted as an estimate? |
|---|--:|---|--:|---|
| Development | 10 | first run, before any fix | 51/80 (64%) | yes (extractor had not been adjusted yet) |
| Holdout | 8 | first run, **before any change made after seeing it** | **47/64 (73%)** | **yes: this is the unbiased headline** |
| Development | 10 | current extractor | see below | **no**: rules were fixed using these papers |
| Holdout | 8 | current extractor | see below | **no longer**: general defects it exposed were then fixed |

Only the 73% is an honest estimate for new papers. The "current" tables show the extractor has not regressed
and how it behaves now, but they are optimistic. Ten abstracts per set is a tiny sample; treat differences of
a few points as noise. A fresh, unseen set is needed before quoting a new accuracy figure.
"""


def full_report() -> str:
    parts = ["# Evidence extraction results (generated by run_evidence_eval.py)", "", HISTORY]
    for title, name in (("Development set (tuned on: optimistic)", "evidence_gold.json"), ("Holdout set (fixes were made after its first measurement)", "evidence_holdout.json")):
        parts += [f"## {title}", "", render(evaluate(name)), ""]
    return "\n".join(parts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--holdout", action="store_true", help="score the holdout set instead of the development set")
    parser.add_argument("--write", action="store_true", help="score BOTH sets and write evaluation/EVIDENCE_RESULTS.md")
    args = parser.parse_args()
    if args.write:
        (HERE / "EVIDENCE_RESULTS.md").write_text(full_report() + "\n", encoding="utf-8")
        print("wrote", HERE / "EVIDENCE_RESULTS.md")
    else:
        print(render(evaluate("evidence_holdout.json" if args.holdout else "evidence_gold.json")))
