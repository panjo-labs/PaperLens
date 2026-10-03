import json

import pytest

from app.services.evidence.sections import split_sentences
from tests.conftest import FIXTURES

EVAL_DIR = FIXTURES.parent.parent / "evaluation"


def texts(abstract):
    return [s.text for s in split_sentences(abstract)]


def test_structured_abstract_keeps_section_labels_and_splits_sentences():
    abstract = "BACKGROUND: Pain is common. It is costly.\nMETHODS: We ran a trial.\nRESULTS: Pain fell."
    sentences = split_sentences(abstract)

    assert [(s.section, s.text) for s in sentences] == [
        ("BACKGROUND", "Pain is common."),
        ("BACKGROUND", "It is costly."),
        ("METHODS", "We ran a trial."),
        ("RESULTS", "Pain fell."),
    ]


def test_unstructured_abstract_has_no_sections():
    sentences = split_sentences("We studied adults. Pain fell.")
    assert [s.section for s in sentences] == [None, None]


def test_every_sentence_points_back_to_its_exact_place_in_the_abstract():
    abstract = "OBJECTIVE: To test X.\nMETHODS: n = 5.  Extra   spaces here.\nRESULTS: It worked (p < 0.05). Done."
    for s in split_sentences(abstract):
        assert abstract[s.start : s.end] == s.text


@pytest.mark.parametrize(
    "abstract",
    [
        "Background:: Round shoulder posture is common. We tested it.",  # a doubled colon
        "Aims: To compare X.\nMethods: We did.",  # mixed-case labels
        "DESIGN, SETTING, AND PARTICIPANTS: This trial included adults.",  # long label with commas
    ],
)
def test_label_variants_are_recognised_and_offsets_stay_exact(abstract):
    sentences = split_sentences(abstract)
    assert sentences[0].section is not None
    assert all(abstract[s.start : s.end] == s.text for s in sentences)


def test_a_line_without_a_label_continues_the_previous_section():
    abstract = "Background: First paragraph.\nSecond paragraph of the same section."
    assert [s.section for s in split_sentences(abstract)] == ["Background", "Background"]


def test_sentences_are_not_split_at_abbreviations_decimals_or_initials():
    assert texts("We compared Pilates vs. home exercise. Pain fell by 2.5 points.") == [
        "We compared Pilates vs. home exercise.",
        "Pain fell by 2.5 points.",
    ]
    assert texts("Led by J. Smith. Results followed.") == ["Led by J. Smith.", "Results followed."]
    assert texts("Treatments (e.g. Pilates) helped. It was good.") == ["Treatments (e.g. Pilates) helped.", "It was good."]
    assert len(texts("The p value was 0.05 or less in 3.2 of cases.")) == 1


def test_a_colon_inside_a_sentence_is_not_a_section_label():
    sentences = split_sentences("The study was done in Lyon: results are below.")
    assert sentences[0].section is None and len(sentences) == 1


def test_empty_and_whitespace_abstracts_give_no_sentences():
    assert split_sentences("") == []
    assert split_sentences("  \n \n") == []


def test_blank_lines_and_extra_spaces_do_not_create_empty_sentences():
    sentences = split_sentences("METHODS:  Spaced   out.\n\n\nRESULTS:   Done.   ")
    assert [s.text for s in sentences] == ["Spaced   out.", "Done."]


def test_offsets_hold_for_every_real_abstract_in_the_snapshot():
    pools = json.loads((EVAL_DIR / "pools.json").read_text(encoding="utf-8"))
    checked = 0
    for entry in pools["questions"].values():
        for provider in entry["providers"].values():
            for paper in provider:
                if paper["abstract"]:
                    checked += 1
                    for s in split_sentences(paper["abstract"]):
                        assert paper["abstract"][s.start : s.end] == s.text
    assert checked > 200
