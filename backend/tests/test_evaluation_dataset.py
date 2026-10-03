"""Guards the evaluation dataset in backend/evaluation (it must stay trustworthy and runnable)."""

import json
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parent.parent / "evaluation"
sys.path.insert(0, str(EVAL_DIR))  # so `run_eval` can be imported

import run_eval  # noqa: E402


@pytest.fixture(scope="module")
def pools() -> dict:
    return json.loads((EVAL_DIR / "pools.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def judgments() -> dict:
    return json.loads((EVAL_DIR / "judgments.json").read_text(encoding="utf-8"))


def test_there_are_ten_questions_with_snapshots_from_both_providers(pools):
    assert len(pools["questions"]) == 10
    for qid, entry in pools["questions"].items():
        assert entry["providers"]["pubmed"] and entry["providers"]["crossref"], qid


def test_every_judged_relevant_paper_was_really_retrieved(pools, judgments):
    """Protects against typos or invented ids: a judged paper must exist in that question's snapshot."""
    assert judgments["questions"].keys() == pools["questions"].keys()
    for qid, entry in pools["questions"].items():
        retrieved = {d["id"]: d["title"] for papers in entry["providers"].values() for d in papers}
        relevant = judgments["questions"][qid]["relevant"]
        assert 5 <= len(relevant) <= 12, qid  # a small set, as intended
        assert len({r["id"] for r in relevant}) == len(relevant), f"{qid}: duplicate judged id"
        for r in relevant:
            assert r["id"] in retrieved, f"{qid}: {r['id']} was never retrieved"
            assert retrieved[r["id"]] == r["title"], f"{qid}: title mismatch for {r['id']}"


def test_evaluation_runs_offline_and_is_deterministic():
    first, second = run_eval.evaluate(), run_eval.evaluate()

    assert first == second
    assert len(first["rows"]) == 10
    for row in first["rows"]:
        for system in ("baseline", "ranked"):
            for value in row[system].values():
                assert 0.0 <= value <= 1.0
        # every judged paper is found somewhere in the final list
        assert len(row["ranked_positions"]) == row["relevant"]


def test_ranker_has_not_collapsed_below_a_sanity_floor():
    """A tripwire for breakage, NOT a quality claim: the baseline ranker currently scores well above this."""
    mean = run_eval.evaluate()["mean"]["ranked"]
    assert mean["recall@10"] >= 0.30
    assert mean["recall@20"] >= 0.45
    assert mean["mrr"] >= 0.35


def test_metric_helpers():
    assert run_eval.metrics([1, 6], 4) == {"recall@5": 0.25, "recall@10": 0.5, "recall@20": 0.5, "mrr": 1.0}
    assert run_eval.metrics([], 3)["mrr"] == 0.0
    assert run_eval.metrics([7], 1)["mrr"] == pytest.approx(1 / 7)
