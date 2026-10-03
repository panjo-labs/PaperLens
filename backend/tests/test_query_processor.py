import pytest

from app.services.query_processor import InvalidQueryError, process_question


def q(question: str) -> str:
    return process_question(question).search_query


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "What is the effectiveness of exercise therapy for chronic low back pain?",
            "effectiveness exercise therapy chronic low back pain",
        ),
        (
            "Does exercise therapy improve pain and function in people with chronic low back pain?",
            "exercise therapy improve pain function people chronic low back pain",
        ),
        (
            "Please tell me about the role of MRI in diagnosing knee injuries.",
            "role MRI diagnosing knee injuries",
        ),
    ],
)
def test_question_and_prompt_words_are_removed(question, expected):
    assert q(question) == expected


def test_domain_terms_hyphens_and_case_are_preserved():
    assert q("Is COVID-19 vaccination linked to Guillain-Barré syndrome?") == (
        "COVID-19 vaccination linked Guillain-Barré syndrome"
    )


def test_apostrophes_inside_words_survive_but_punctuation_does_not():
    assert q("How does Parkinson’s disease (PD) progress; any \"biomarkers\"?") == (
        "Parkinson's disease PD progress any biomarkers"
    )


def test_boolean_operators_cannot_leak_into_the_query():
    result = q("Exercise AND pain OR NOT stretching")
    assert result == "Exercise pain or not stretching"
    assert "OR" not in result.split() and "NOT" not in result.split()


def test_field_tag_and_phrase_syntax_is_stripped():
    assert q('smith[Author] "low back pain"[tiab]') == "smith Author low back pain tiab"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What do WHO guidelines recommend for hypertension in the US?", "WHO guidelines recommend hypertension US"),
        ("Does vitamin A supplementation reduce child mortality?", "vitamin A supplementation reduce child mortality"),
        ("Is type I diabetes linked to hepatitis A infection?", "type I diabetes linked hepatitis A infection"),
        ("A randomized trial of who gets help?", "randomized trial gets help"),  # leading "A", lower-case "who"
        ("WHAT IS THE EFFECT OF YOGA?", "EFFECT YOGA"),  # all-caps question: no case signal
    ],
)
def test_acronyms_that_collide_with_stopwords_are_preserved(question, expected):
    assert q(question) == expected


def test_original_question_is_kept_trimmed():
    processed = process_question("  What causes migraine?  ")
    assert processed.original == "What causes migraine?"
    assert processed.search_query == "causes migraine"


def test_is_deterministic():
    question = "What is the effect of yoga on anxiety?"
    assert process_question(question) == process_question(question)


@pytest.mark.parametrize("question", ["What is it?", "???", "how do you", ""])
def test_question_without_content_words_is_rejected(question):
    with pytest.raises(InvalidQueryError):
        process_question(question)
