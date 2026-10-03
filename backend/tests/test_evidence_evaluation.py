"""Protects the evidence evaluation data and runner (backend/evaluation)."""

import json
import sys

import pytest

from tests.conftest import FIXTURES

EVAL_DIR = FIXTURES.parent.parent / "evaluation"
sys.path.insert(0, str(EVAL_DIR))

import run_evidence_eval as ev_eval  # noqa: E402
from app.schemas.evidence import Evidence  # noqa: E402

FIELDS = ["study_design", "participants", "studies", "population", "intervention", "comparator", "outcomes", "statistics", "limitations_stated", "notes", "id"]


def load(name):
    return json.loads((EVAL_DIR / name).read_text(encoding="utf-8"))["papers"]


@pytest.fixture(scope="module")
def snapshot():
    return ev_eval.load_papers()


@pytest.fixture(scope="module")
def results():
    """Each set evaluated ONCE per test module (an evaluation takes a couple of seconds)."""
    return {name: ev_eval.evaluate(name, latency=False) for name in ("evidence_gold.json", "evidence_holdout.json")}


@pytest.mark.parametrize("name", ["evidence_gold.json", "evidence_holdout.json"])
def test_every_gold_paper_was_really_retrieved_and_has_an_abstract(name, snapshot):
    for gold in load(name):
        assert gold["id"] in snapshot, f"{gold['id']} was never retrieved"
        assert snapshot[gold["id"]].abstract, f"{gold['id']} has no abstract, so nothing could be established"


@pytest.mark.parametrize("name", ["evidence_gold.json", "evidence_holdout.json"])
def test_gold_entries_are_complete_and_well_formed(name):
    for gold in load(name):
        assert set(gold) == set(FIELDS), gold["id"]
        assert isinstance(gold["limitations_stated"], bool)
        for key in ("population", "intervention", "comparator", "outcomes"):
            assert gold[key] is None or (isinstance(gold[key], list) and gold[key]), (gold["id"], key)
        assert gold["notes"].strip()


def test_the_holdout_never_overlaps_the_development_set():
    dev, holdout = {g["id"] for g in load("evidence_gold.json")}, {g["id"] for g in load("evidence_holdout.json")}
    assert dev and holdout and not (dev & holdout)
    assert len(dev) == 10 and len(holdout) == 8


def test_the_gold_values_were_written_for_papers_that_exist_with_the_text_they_describe(snapshot):
    """A cheap check that expected keywords/statistics are not typos: each must occur in the abstract or title."""
    squash = lambda t: "".join(t.split()).lower()  # noqa: E731
    for name in ("evidence_gold.json", "evidence_holdout.json"):
        for gold in load(name):
            paper = snapshot[gold["id"]]
            text = squash(f"{paper.title} {paper.abstract}")
            for stat in gold["statistics"]:
                assert squash(stat) in text, f"{gold['id']}: statistic {stat!r} is not in the abstract"
            for key in ("population", "intervention", "comparator", "outcomes"):
                for keyword in gold[key] or []:
                    assert squash(keyword) in text, f"{gold['id']}: keyword {keyword!r} ({key}) is not in the paper"


@pytest.mark.parametrize("name", ["evidence_gold.json", "evidence_holdout.json"])
def test_evaluation_runs_offline_and_is_deterministic(name, results):
    assert ev_eval.evaluate(name, latency=False) == results[name]  # an independent second run (timings are skipped: they legitimately differ)


@pytest.mark.parametrize("name", ["evidence_gold.json", "evidence_holdout.json"])
def test_no_extracted_value_is_ever_absent_from_the_text_it_cites(name, results):
    audit = results[name]["traceability"]
    assert audit["papers"] > 380 and audit["values_checked"] > 1500
    assert audit["violations"] == []


def test_the_holdout_has_no_fabricated_or_wrong_values_except_the_one_known_design_case(results):
    """'Fabricated' = a value where the abstract says nothing. Not one is allowed on unseen-style data."""
    result = results["evidence_holdout.json"]
    assert all(counts.get("fabricated", 0) == 0 for counts in result["per_field"].values())
    wrong = [p for p in result["problems"] if p["outcome"] == "wrong"]
    assert [(p["id"], p["field"]) for p in wrong] == [("pubmed:25818837", "study_design")]  # needs the title's 'meta-analysis'


def test_accuracy_has_not_collapsed_below_a_sanity_floor(results):
    """A tripwire for breakage, NOT a quality claim (see EVIDENCE_RESULTS.md for the honest numbers)."""
    for name in ("evidence_gold.json", "evidence_holdout.json"):
        result = results[name]
        correct = sum(c.get("correct", 0) + c.get("correct-null", 0) for c in result["per_field"].values())
        total = sum(sum(c.values()) for c in result["per_field"].values())
        assert correct / total >= 0.6, name
        assert result["statistics"]["found"] / result["statistics"]["expected"] >= 0.8, name


def test_judge_classifies_each_outcome():
    judge = ev_eval.judge
    assert judge(None, None, True) == "correct-null"
    assert judge(None, "something", True) == "fabricated"
    assert judge(["pain"], None, True) == "missed"
    assert judge(["pain"], "Pain intensity | disability", True) == "correct"  # case-insensitive keywords
    assert judge(["pain", "sleep"], "pain only", True) == "wrong"  # ALL keywords are required
    assert judge(75, 75, False) == "correct"
    assert judge(75, 74, False) == "wrong"
    assert judge(75, None, False) == "missed"
    assert judge(None, 5, False) == "fabricated"


def test_the_scorer_compares_statistics_ignoring_spacing_and_case():
    gold = {"statistics": ["P = .65", "95% CI, -0.38 to 0.23", "99%"]}
    evidence = Evidence(paper_id="p:1", extractor="x", abstract_status="present")
    assert ev_eval.statistics_recall(gold, evidence) == (0, 3)  # nothing extracted -> nothing found

    from app.services.evidence.rule_based import RuleBasedEvidenceExtractor
    from app.schemas.paper import Paper

    paper = Paper(id="p:1", title="T", authors=[], abstract="RESULTS: The difference was -0.07 (95% CI, -0.38 to 0.23; p = .65).", source="x", source_id="1")
    found, expected = ev_eval.statistics_recall(gold, RuleBasedEvidenceExtractor().extract_sync(paper))
    assert (found, expected) == (2, 3)  # the CI and "p = .65" (same as "P = .65" ignoring case) are found; "99%" is not in the text
