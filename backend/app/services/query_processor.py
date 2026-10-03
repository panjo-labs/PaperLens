"""Deterministic research-question -> search-query normalization.

No LLM, no synonym expansion, no PICO extraction. We only drop words that carry no
search content (question words, auxiliaries, articles, pronouns, prompt verbs, connectives)
and punctuation, and keep everything else, including case, so acronyms like "MRI" survive.

Upper-case tokens that collide with stopwords (WHO, US, IT, "vitamin A") are kept.

Limitation: quotes and parentheses are removed, so users cannot supply phrase or field syntax.

Example:
    "What is the effectiveness of exercise therapy for chronic low back pain?"
    -> "effectiveness exercise therapy chronic low back pain"
"""

import re
import unicodedata
from dataclasses import dataclass

# A "word": letters/digits, optionally joined by - or ' (so "COVID-19" and "Parkinson's" stay whole).
# Everything else (?, commas, brackets, quotes...) is simply not matched, i.e. dropped.
_TOKEN = re.compile(r"\w+(?:['-]\w+)*")

# Words that say nothing about the *topic* ("what is the ...", "please tell me about ...").
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
# "or" / "not" are kept (dropping "or" would change the meaning), but forced to lowercase.
# In PubMed only UPPERCASE AND/OR/NOT are search operators, so lowercase can't be misread as one.
_OPERATOR_WORDS = frozenset({"or", "not"})
_BOOLEAN_WORDS = _OPERATOR_WORDS | {"and"}


class InvalidQueryError(ValueError):
    """The question contains no searchable terms."""


@dataclass(frozen=True)
class ProcessedQuery:
    original: str  # what the user typed (trimmed)
    search_query: str  # what we actually send to the search APIs


def _is_acronym(token: str, index: int, shouting: bool) -> bool:
    """Upper-case tokens that collide with stopwords (WHO, US, IT, the A in "vitamin A")."""
    if shouting or not token.isupper() or token.lower() in _BOOLEAN_WORDS:
        return False  # boolean words must never survive in upper case: PubMed would read them as operators
    # A lone capital is only an acronym mid-sentence; at the start it is "A"/"I" the word.
    return len(token) > 1 or index > 0


def process_question(question: str) -> ProcessedQuery:
    # Normalize fancy Unicode (e.g. full-width letters) and curly apostrophes to plain ones.
    text = unicodedata.normalize("NFKC", question).replace("’", "'")
    # If the whole question is UPPERCASE, capitals tell us nothing about acronyms.
    shouting = not any(ch.islower() for ch in text)
    terms: list[str] = []
    for index, token in enumerate(_TOKEN.findall(text)):
        lowered = token.lower()
        # Skip stopwords ("what", "the"...) unless it's really an acronym such as "WHO".
        if lowered in _STOPWORDS and not _is_acronym(token, index, shouting):
            continue
        terms.append(lowered if lowered in _OPERATOR_WORDS else token)

    if not terms:
        # e.g. "What is it?" has nothing left to search for.
        raise InvalidQueryError("The question contains no searchable terms.")
    return ProcessedQuery(original=question.strip(), search_query=" ".join(terms))
