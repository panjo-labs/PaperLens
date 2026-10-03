"""Paper ranker: orders papers by how well their words match the research question.

This is a deliberately simple, explainable baseline. It is a RETRIEVAL HEURISTIC: it measures
word overlap, not scientific relevance or quality. No LLM, no embeddings, no network calls,
and no citation counts.

How a paper is scored
---------------------
1. Take the question's words, drop stopwords ("the", "what", "of"...) and reduce plurals
   ("exercises" -> "exercise"), so we have a set of N distinct search terms.
2. TITLE coverage    = (search terms found in the title) / N.
3. ABSTRACT coverage = for each search term, 1/3 credit per mention in the abstract, capped at
                       3 mentions (so repeating a word 50 times is not worth more than 3), then
                       averaged over the N terms.
4. score = 0.75 * title coverage + 0.25 * abstract coverage        (always between 0 and 1)

So a title containing every term (0.75) beats an abstract that mentions every term three
times (0.25), while a paper matching in both places scores highest. A paper without an abstract
simply gets 0 abstract coverage; it is not penalised in any other way.

Not used (on purpose): journal name, author keywords/MeSH subjects (our records don't carry them
yet), and any popularity signal. Publication date and abstract presence are only used to break ties.

Tie-breaking (fully deterministic): higher score, then has an abstract, then newer date, then id.

Replacing this later: only `rank_papers(query, papers) -> list[Paper]` is used by the rest of
the app, so a better ranker can be swapped in without touching the providers.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

from app.schemas.paper import Paper
from app.services.query_processor import STOPWORDS

TITLE_WEIGHT = 0.75
ABSTRACT_WEIGHT = 0.25
ABSTRACT_FULL_CREDIT_MENTIONS = 3  # mentions needed for a term to earn full abstract credit

_WORD = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    """Very crude plural removal so "exercises" matches "exercise". Applied to BOTH sides, so
    its mistakes (e.g. "diabetes" -> "diabete") cancel out."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"  # injuries -> injury
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]  # patients -> patient
    return word


def _words(text: str | None) -> list[str]:
    """Lowercase, accent-free, stemmed words of `text` (stopwords NOT removed here)."""
    if not text:
        return []
    plain = text
    if not text.isascii():  # accent-stripping is slow and only needed for non-ASCII text
        decomposed = unicodedata.normalize("NFKD", text)
        plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    plain = plain.lower()
    # "high-intensity" -> "high", "intensity". Keep numbers ("type 2"), drop 1-letter words.
    return [_stem(w) for w in _WORD.findall(plain) if len(w) > 1 or w.isdigit()]


def query_terms(query: str) -> set[str]:
    """The distinct, meaningful words of the question (the "N search terms" above)."""
    return {w for w in _words(query) if w not in STOPWORDS}


def score_paper(terms: set[str], paper: Paper) -> float:
    """Score one paper against already-prepared search terms. Returns a number in [0, 1]."""
    if not terms:
        return 0.0

    title_words = set(_words(paper.title))
    title_coverage = len(terms & title_words) / len(terms)

    abstract_counts = Counter(_words(paper.abstract))  # {} when there is no abstract
    abstract_coverage = (
        sum(min(abstract_counts[t], ABSTRACT_FULL_CREDIT_MENTIONS) for t in terms)
        / ABSTRACT_FULL_CREDIT_MENTIONS
        / len(terms)
    )

    # Rounded so tiny floating-point noise can never reorder papers that are really tied.
    return round(TITLE_WEIGHT * title_coverage + ABSTRACT_WEIGHT * abstract_coverage, 6)


def _date_key(date: str | None) -> tuple[int, int, int]:
    """"2024-03" -> (2024, 3, 0). Unknown or malformed dates sort as the oldest possible."""
    try:
        parts = [int(p) for p in (date or "").split("-")]
    except ValueError:
        return (0, 0, 0)
    return tuple((parts + [0, 0, 0])[:3])  # type: ignore[return-value]


def rank_papers(query: str, papers: Sequence[Paper]) -> list[Paper]:
    """Return copies of `papers`, best match first, each with `rank_score` filled in."""
    terms = query_terms(query)  # prepared once, reused for every paper
    scored = [paper.model_copy(update={"rank_score": score_paper(terms, paper)}) for paper in papers]

    def order(paper: Paper) -> tuple:
        year, month, day = _date_key(paper.publication_date)
        return (
            -(paper.rank_score or 0.0),  # 1. higher score first
            not paper.abstract,  # 2. papers with an abstract first (False sorts before True)
            (-year, -month, -day),  # 3. newer first
            paper.id,  # 4. alphabetical id: makes the order fully repeatable
        )

    return sorted(scored, key=order)
