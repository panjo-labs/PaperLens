"""Lightweight retrieval evaluation for the dedup + ranking pipeline (offline, deterministic).

    python evaluation/run_eval.py              # summary table
    python evaluation/run_eval.py --verbose    # also show each question's top 10 (✓ = judged relevant)
    python evaluation/run_eval.py --write      # also write evaluation/RESULTS.md

What it does, for each question in pools.json:
  1. take the SNAPSHOT of what PubMed and Crossref returned (no network),
  2. deduplicate, then compare two orderings:
       baseline = provider order (PubMed's 20, then Crossref's 20)  <- what Slice 2A returned
       ranked   = our lexical ranker                                  <- Slice 2B
  3. look up where the papers judged relevant (judgments.json) end up.

Metrics (higher is better):
  recall@k = share of the relevant papers that appear in the top k.
  MRR      = 1 / rank of the first relevant paper (1.0 = a relevant paper is first).

Limits worth remembering: relevance is judged only among papers that were retrieved, so this
measures RANKING, not whether the providers found everything; the judgments are one person's
reading of titles/abstracts; and with ~8 relevant papers per question the numbers are rough.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # make `app` importable

from app.schemas.paper import Paper  # noqa: E402
from app.services.deduplicator import deduplicate, normalize_title  # noqa: E402
from app.services.ranker import rank_papers  # noqa: E402

KS = (5, 10, 20)


def load() -> tuple[dict, dict]:
    pools = json.loads((HERE / "pools.json").read_text(encoding="utf-8"))
    judgments = json.loads((HERE / "judgments.json").read_text(encoding="utf-8"))
    return pools, judgments


def relevant_ranks(ordered: list[Paper], relevant_ids: set[str]) -> list[int]:
    """1-based positions of papers that match any judged-relevant id (merged papers match via source_ids)."""
    return [i for i, p in enumerate(ordered, 1) if relevant_ids & set(p.source_ids)]


def metrics(ranks: list[int], n_relevant: int) -> dict:
    return {
        **{f"recall@{k}": sum(r <= k for r in ranks) / n_relevant for k in KS},
        "mrr": 1 / ranks[0] if ranks else 0.0,
    }


def unmerged_same_title_pairs(unique: list[Paper]) -> int:
    """Papers left unmerged although another paper has the identical normalized title (a dedup blind spot)."""
    by_title = defaultdict(list)
    for p in unique:
        title = normalize_title(p.title)
        if title:
            by_title[title].append(p)
    return sum(len(group) - 1 for group in by_title.values() if len(group) > 1)


def evaluate(verbose: bool = False) -> dict:
    pools, judgments = load()
    rows = []
    for qid, entry in pools["questions"].items():
        relevant = {r["id"] for r in judgments["questions"][qid]["relevant"]}
        raw = [Paper(**d) for provider in ("pubmed", "crossref") for d in entry["providers"][provider]]
        unique = deduplicate(raw)  # provider order = the baseline ordering
        ranked = rank_papers(entry["query"], unique)

        base_ranks = relevant_ranks(unique, relevant)
        rank_ranks = relevant_ranks(ranked, relevant)
        assert len(base_ranks) == len(relevant) == len(rank_ranks), f"{qid}: a judged paper is missing from the pool"

        rows.append(
            {
                "id": qid,
                "question": entry["question"],
                "raw": len(raw),
                "unique": len(unique),
                "relevant": len(relevant),
                "baseline": metrics(base_ranks, len(relevant)),
                "ranked": metrics(rank_ranks, len(relevant)),
                "ranked_positions": rank_ranks,
                "unmerged_same_title": unmerged_same_title_pairs(unique),
            }
        )
        if verbose:
            print(f"\n{qid}: {entry['question']}\n    query: {entry['query']!r}")
            for i, p in enumerate(ranked[:10], 1):
                mark = "✓" if relevant & set(p.source_ids) else " "
                print(f"  {i:2d}. {mark} {p.rank_score:.3f} {p.title[:95]}")
            print(f"      relevant papers end up at ranks {rank_ranks}")

    def mean(system: str, key: str) -> float:
        return sum(r[system][key] for r in rows) / len(rows)

    return {
        "recorded_on": pools["recorded_on"],
        "rows": rows,
        "mean": {s: {m: mean(s, m) for m in ("recall@5", "recall@10", "recall@20", "mrr")} for s in ("baseline", "ranked")},
    }


def render(result: dict) -> str:
    lines = [
        f"Snapshot recorded {result['recorded_on']}  |  baseline = dedup only, provider order  |  ranked = Slice 2B ranker",
        "",
        "| Q | raw | unique | rel | recall@5 base→ranked | recall@10 base→ranked | recall@20 base→ranked | MRR base→ranked |",
        "|---|----:|-------:|----:|:--------------------:|:---------------------:|:---------------------:|:---------------:|",
    ]
    for r in result["rows"]:
        b, k = r["baseline"], r["ranked"]
        cells = " | ".join(f"{b[m]:.2f} → {k[m]:.2f}" for m in ("recall@5", "recall@10", "recall@20", "mrr"))
        lines.append(f"| {r['id']} | {r['raw']} | {r['unique']} | {r['relevant']} | {cells} |")
    m = result["mean"]
    mean_cells = " | ".join(f"**{m['baseline'][x]:.2f} → {m['ranked'][x]:.2f}**" for x in ("recall@5", "recall@10", "recall@20", "mrr"))
    lines.append(f"| **mean** | | | | {mean_cells} |")
    unmerged = sum(r["unmerged_same_title"] for r in result["rows"])
    lines += ["", f"Papers left unmerged despite an identical normalized title (dedup blind spot): {unmerged} across all questions."]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--write", action="store_true", help="write the table to evaluation/RESULTS.md")
    args = parser.parse_args()

    result = evaluate(args.verbose)
    table = render(result)
    print("\n" + table)
    if args.write:
        (HERE / "RESULTS.md").write_text("# Evaluation results (generated by run_eval.py)\n\n" + table + "\n", encoding="utf-8")
        print("\nwrote", HERE / "RESULTS.md")
