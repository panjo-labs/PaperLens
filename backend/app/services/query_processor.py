"""Deterministic research-question -> search-query normalization.

No LLM, no synonym expansion, no PICO extraction. We only drop words that carry no
search content (question words, auxiliaries, articles, pronouns, prompt verbs, connectives)
and punctuation, and keep everything else, including case, so acronyms like "MRI" survive.

Upper-case tokens that collide with stopwords (WHO, US, IT, "vitamin A") are kept.

Limitation: quotes and parentheses are removed, so users cannot supply phrase or field syntax.
"""

import re
import unicodedata
from dataclasses import dataclass

_TOKEN = re.compile(r"\w+(?:['-]\w+)*")

_STOPWORDS = frozenset(
    """
    what what's which who whom whose when where why how
    is are was were be been being am do does did done can could will would should shall may might must
    has have had having there their theirs it its this that these those
    i me my we us our you your
    the a an
    of in on at for to with by from as about into
    and
    please tell give show find list explain describe summarize summarise search look know want need
    """.split()
)
# Kept as plain lowercase words so they can never be read as PubMed boolean operators.
_OPERATOR_WORDS = frozenset({"or", "not"})
_BOOLEAN_WORDS = _OPERATOR_WORDS | {"and"}


class InvalidQueryError(ValueError):
    """The question contains no searchable terms."""


@dataclass(frozen=True)
class ProcessedQuery:
    original: str
    search_query: str


def _is_acronym(token: str, index: int, shouting: bool) -> bool:
    """Upper-case tokens that collide with stopwords (WHO, US, IT, the A in "vitamin A")."""
    if shouting or not token.isupper() or token.lower() in _BOOLEAN_WORDS:
        return False  # boolean words must never survive in upper case: PubMed would read them as operators
    # A lone capital is only an acronym mid-sentence; at the start it is "A"/"I" the word.
    return len(token) > 1 or index > 0


def process_question(question: str) -> ProcessedQuery:
    text = unicodedata.normalize("NFKC", question).replace("’", "'")
    shouting = not any(ch.islower() for ch in text)  # an ALL-CAPS question has no case signal
    terms: list[str] = []
    for index, token in enumerate(_TOKEN.findall(text)):
        lowered = token.lower()
        if lowered in _STOPWORDS and not _is_acronym(token, index, shouting):
            continue
        terms.append(lowered if lowered in _OPERATOR_WORDS else token)

    if not terms:
        raise InvalidQueryError("The question contains no searchable terms.")
    return ProcessedQuery(original=question.strip(), search_query=" ".join(terms))
