import itertools
import random

import pytest

from app.schemas.paper import Paper
from app.services.ranker import query_terms, rank_papers, score_paper


def paper(pid="1", title="Unrelated title", abstract=None, date=None, **extra) -> Paper:
    return Paper(
        id=f"pubmed:{pid}", title=title, authors=[], abstract=abstract, publication_date=date,
        source="pubmed", source_id=pid, **extra,
    )


def ids(papers) -> list[str]:
    return [p.source_id for p in papers]


QUESTION = "effect of exercise therapy on chronic low back pain"


# --- query terms ------------------------------------------------------------------------------


def test_stopwords_and_one_letter_words_are_not_search_terms():
    assert query_terms("What is the effect of exercise on pain?") == {"effect", "exercise", "pain"}


def test_plurals_hyphens_and_accents_are_normalized():
    assert query_terms("Exercises for high-intensity Guillain-Barré injuries") == {
        "exercise", "high", "intensity", "guillain", "barre", "injury",
    }


def test_numbers_are_kept():
    assert "2" in query_terms("type 2 diabetes")


def test_repeated_query_words_count_once():
    assert query_terms("pain pain PAIN") == {"pain"}


# --- scoring ----------------------------------------------------------------------------------


def test_score_formula_on_simple_cases():
    terms = query_terms("exercise pain")

    assert score_paper(terms, paper(title="Exercise and pain")) == 0.75  # all terms in title
    assert score_paper(terms, paper(abstract="exercise pain")) == pytest.approx(0.083333)  # 1 mention each
    assert score_paper(terms, paper(abstract="exercise pain " * 3)) == 0.25  # 3 mentions each: full credit
    assert score_paper(terms, paper(title="Exercise pain", abstract="exercise pain " * 3)) == 1.0
    assert score_paper(terms, paper()) == 0.0


def test_title_match_ranks_above_abstract_only_match():
    title_hit = paper("title", title="Exercise therapy for chronic low back pain")
    abstract_hit = paper(
        "abstract", title="A study of patients",
        abstract=" ".join(["exercise therapy chronic low back pain"] * 5),
    )
    ranked = rank_papers(QUESTION, [abstract_hit, title_hit])

    assert ids(ranked) == ["title", "abstract"]
    assert ranked[0].rank_score > ranked[1].rank_score


def test_title_and_abstract_match_beats_title_only():
    both = paper("both", title="Exercise therapy back pain", abstract="exercise therapy back pain study")
    title_only = paper("title", title="Exercise therapy back pain")
    assert ids(rank_papers(QUESTION, [title_only, both])) == ["both", "title"]


def test_more_matching_terms_increase_the_score():
    terms = query_terms(QUESTION)
    scores = [
        score_paper(terms, paper(title=title))
        for title in ["Cats", "Exercise", "Exercise therapy", "Exercise therapy low back pain"]
    ]
    assert scores == sorted(scores) and len(set(scores)) == 4


def noise_score_only_from_effect(noise: Paper) -> bool:
    """With the real question, the noise paper matches 1 of 3 terms ("effect"): title 1/3 only."""
    terms = query_terms("What is the effect of exercise therapy on the back?")
    return len(terms) == 4 and score_paper(terms, noise) == pytest.approx(0.75 * 1 / 4)


def test_stopwords_do_not_dominate_the_ranking():
    # This title is full of the question's stopwords but has none of its topic words.
    noise = paper("noise", title="What is the effect of it on the", abstract="of the and is what " * 20)
    topic = paper("topic", title="Exercise therapy")
    ranked = rank_papers("What is the effect of exercise therapy on the back?", [noise, topic])

    assert ids(ranked) == ["topic", "noise"]
    # Even a stopword-stuffed paper scores exactly 0 against a question of only stopwords.
    assert score_paper(query_terms("what is the of it on"), noise) == 0.0
    # And its score against the real question comes only from "effect", not from the stopwords.
    assert noise_score_only_from_effect(noise)


def test_a_question_made_only_of_stopwords_scores_everything_zero():
    ranked = rank_papers("what is the", [paper("a", title="What is the"), paper("b")])
    assert [p.rank_score for p in ranked] == [0.0, 0.0]


def test_repeating_a_word_many_times_is_capped():
    terms = query_terms("exercise")
    three = score_paper(terms, paper(abstract="exercise exercise exercise"))
    fifty = score_paper(terms, paper(abstract="exercise " * 50))
    assert three == fifty == 0.25


def test_plural_and_hyphen_variants_match():
    terms = query_terms("high-intensity exercise injuries")
    assert score_paper(terms, paper(title="High intensity exercises: injury risk")) == 0.75


def test_matching_ignores_case_and_accents():
    assert score_paper(query_terms("Guillain-Barre syndrome"), paper(title="GUILLAIN-BARRÉ SYNDROME")) == 0.75


def test_score_is_always_between_zero_and_one():
    p = paper(title=QUESTION, abstract=(QUESTION + " ") * 30)
    assert 0.0 <= score_paper(query_terms(QUESTION), p) <= 1.0


# --- missing / odd metadata ------------------------------------------------------------------


def test_missing_abstract_none_and_empty_string_behave_the_same():
    terms = query_terms(QUESTION)
    assert score_paper(terms, paper(abstract=None)) == score_paper(terms, paper(abstract="")) == 0.0


def test_paper_with_everything_missing_does_not_crash():
    bare = Paper(id="x:1", title="", authors=[], source="x", source_id="1")
    ranked = rank_papers(QUESTION, [bare])
    assert ranked[0].rank_score == 0.0


@pytest.mark.parametrize("date", [None, "", "2024", "2024-03", "2024-03-15", "garbage", "20x4-01"])
def test_any_date_value_is_handled(date):
    assert len(rank_papers(QUESTION, [paper("a", date=date), paper("b")])) == 2


def test_empty_input_gives_empty_output():
    assert rank_papers(QUESTION, []) == []


def test_inputs_are_not_modified_and_scores_are_attached_to_copies():
    original = paper("a", title="Exercise therapy")
    ranked = rank_papers(QUESTION, [original])

    assert original.rank_score is None
    assert ranked[0].rank_score is not None and ranked[0] is not original


# --- determinism and ties ---------------------------------------------------------------------


def test_ranking_is_deterministic_for_any_input_order():
    papers = [
        paper("a", title="Exercise therapy back pain", abstract="chronic", date="2020"),
        paper("b", title="Exercise therapy back pain", abstract="chronic", date="2020"),  # exact tie with a
        paper("c", title="Back pain", date="2023"),
        paper("d", title="Exercise", abstract="low back pain", date="2019-05"),
        paper("e", title="Unrelated"),
        paper("f", title="Unrelated", date="2022"),
    ]
    expected = ids(rank_papers(QUESTION, papers))

    for _ in range(25):
        shuffled = random.Random(_).sample(papers, len(papers))
        assert ids(rank_papers(QUESTION, shuffled)) == expected
    assert ids(rank_papers(QUESTION, list(reversed(papers)))) == expected


def test_ties_are_broken_by_abstract_then_newer_date_then_id():
    # All four have the same score (0): no query words anywhere.
    with_abstract_old = paper("d", abstract="text", date="2000")
    with_abstract_new = paper("c", abstract="text", date="2024")
    no_abstract_new = paper("b", date="2024-06-01")
    no_abstract_same = paper("a", date="2024-06-01")  # same date as b -> id decides
    undated = paper("e")

    ranked = rank_papers("zzz", [undated, no_abstract_new, with_abstract_old, no_abstract_same, with_abstract_new])

    assert ids(ranked) == ["c", "d", "a", "b", "e"]


def test_more_precise_same_year_date_sorts_as_newer_than_year_only():
    ranked = rank_papers("zzz", [paper("a", date="2024"), paper("b", date="2024-03")])
    assert ids(ranked) == ["b", "a"]


def test_score_beats_every_tie_breaker():
    low = paper("low", abstract="text", date="2030")  # great tie-breakers, no match
    high = paper("high", title="Exercise", date="1990")  # poor tie-breakers, but matches
    assert ids(rank_papers("exercise", [low, high])) == ["high", "low"]


def test_all_permutations_of_a_small_set_give_one_order():
    papers = [paper("a", title="Pain"), paper("b", title="Pain"), paper("c", title="Back pain"), paper("d")]
    orders = {tuple(ids(rank_papers("back pain", list(perm)))) for perm in itertools.permutations(papers)}
    assert len(orders) == 1
