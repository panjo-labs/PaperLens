# Retrieval evaluation (Slice 2B baseline)

A small, deterministic way to check whether **deduplication + ranking** put useful papers near
the top. It is deliberately lightweight: a baseline we can improve against, not a benchmark.

## Files

| File | What it is |
|---|---|
| `questions.json` | 10 fixed research questions (8 health/physiotherapy, 1 psychology/sleep, 1 education) |
| `pools.json` | **Snapshot** of what real PubMed and Crossref returned for each question on the recorded date (20 papers each). Makes the evaluation offline and repeatable. |
| `judgments.json` | The papers judged relevant (6-10 per question), copied from `pools.json` with their titles |
| `run_eval.py` | Offline runner: `python evaluation/run_eval.py [--verbose] [--write]` |
| `build_pools.py` | One-off script that re-creates `pools.json` from the live APIs (only when you want to refresh the data) |
| `RESULTS.md` | Latest table, written by `run_eval.py --write` |

## How the relevance judgments were made

1. Snapshot first (`build_pools.py`), then judge, then run the ranker, in that order.
2. For each question the deduplicated pool was read **in provider order (unranked)**: title plus the start of the
   abstract. The ranker output was not looked at while judging.
3. **Relevant** = the paper directly studies the question's intervention/exposure in the question's population
   or topic *and* reports results (primary study, systematic review/meta-analysis, evidence summary).
   Protocols, editorials, measurement/mechanism papers and other conditions were left out.
4. Only papers that were actually retrieved are listed. `tests/test_evaluation_dataset.py` fails if a judged id
   is missing from the snapshot or its title does not match.

## Metrics

* **recall@k**: share of the judged-relevant papers that appear in the top k results.
* **MRR**: 1 / rank of the first relevant paper (1.0 means a relevant paper is first).
* Compared against a **baseline**: deduplicated results in provider order (PubMed's 20, then Crossref's 20), which is
  what the API returned before ranking existed.

## Limits (read before trusting the numbers)

* **One reviewer's judgments.** They were made by an AI assistant reading titles/abstracts and have not been
  independently checked. Please review `judgments.json`; disagreements are expected and welcome.
* **Pool-based.** Relevance is judged only among retrieved papers, so this measures *ranking*, not whether the
  providers found everything.
* **Skewed toward PubMed.** 59 of 80 relevant papers are PubMed records, probably because they have abstracts and
  so were easier to verify. That favours the baseline, whose first 20 slots are PubMed's.
* **Tiny sample.** 10 questions and 80 judged papers: differences of a few hundredths are noise.
* Do not tune the ranker against these 10 questions and then treat the result as proof of quality; add new
  questions (or hold some out) before drawing conclusions.

---

# Evidence extraction evaluation (Slice 3)

Separate from the retrieval evaluation above. It checks the rule-based evidence extractor against abstracts that were read by hand.

| File | What it is |
|---|---|
| `evidence_gold.json` | **Development set**: 10 papers drawn at random (seed 2026) from snapshot papers with an abstract, with expected values written from the text before the extractor was run on them |
| `evidence_holdout.json` | **Holdout set**: 8 more papers (seed 7001, none from the development set), expected values written before running the extractor |
| `run_evidence_eval.py` | Offline runner: `python evaluation/run_evidence_eval.py [--holdout] [--write]` |
| `EVIDENCE_RESULTS.md` | Combined report, written by `--write` |

Per field the outcome is one of: **correct**, **correct-null** (the abstract does not say it and the extractor correctly said "unavailable"),
**missed**, **wrong**, **fabricated** (a value where the abstract says nothing: the worst case). The runner also audits **all ~390 snapshot
papers** to confirm that every extracted value literally occurs in the text it cites, and reports field availability and latency.

**Read the results carefully.** The development set was used to fix defects, so its score is optimistic. The holdout's *first* measurement
(before any change made after seeing it) is the honest estimate: 47/64 fields correct (73%). Later holdout numbers are no longer unbiased.
With 8-10 abstracts, differences of a few points are noise, and the expected values are one reviewer's reading: please check them.

