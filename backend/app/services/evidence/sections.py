"""Split an abstract into labelled sections and sentences, remembering exact character offsets.

Our parsers produce abstracts in one of two shapes:
  * structured:   one section per line, "BACKGROUND: text\\nMETHODS: text\\n..."  (PubMed, Crossref)
  * unstructured: plain text with no labels.

Every `Sentence` records where it sits in the original string, so that
`abstract[sentence.start:sentence.end] == sentence.text` always holds. That is what lets evidence
point back to the exact text it was taken from (no invented offsets).
"""

import re
from dataclasses import dataclass

# "METHODS: ", "Background:: " (some journals double the colon). Must start a line, start with a
# capital and be short, so ordinary sentences containing a colon are not mistaken for labels.
_LABEL = re.compile(r"^([A-Z][A-Za-z0-9 /&,'’-]{1,60}?):+[ \t]+")

# A sentence boundary: end punctuation, whitespace, then something that can start a sentence.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[\"'“])")

# Words after which a full stop is NOT the end of a sentence ("vs. placebo", "e.g. Pilates").
_ABBREVIATIONS = frozenset({"vs", "e.g", "i.e", "al", "approx", "no", "fig", "ca", "cf", "dr", "mr", "ms", "st", "inc"})


_LABEL_CONNECTORS = frozenset({"and", "of", "the", "for", "in", "on", "to", "&"})


def _is_label(text: str) -> bool:
    """"METHODS", "Data Sources", "DESIGN, SETTING, AND PARTICIPANTS": yes.  "The study was done in Lyon": no."""
    words = re.findall(r"[A-Za-z0-9'" + chr(0x2019) + r"]+", text)
    if len(words) <= 3:
        return True  # a short heading, e.g. "Search methods"
    # a longer heading has capitalised words (or small connectors), unlike an ordinary sentence
    return len(words) <= 8 and all(w[0].isupper() or w.lower() in _LABEL_CONNECTORS for w in words)


@dataclass(frozen=True)
class Sentence:
    text: str
    start: int  # offset into the abstract string
    end: int
    section: str | None  # the abstract section label it belongs to, as written ("METHODS"), or None


def _is_abbreviation_end(before: str) -> bool:
    """True if the text before a full stop ends in an abbreviation or a lone initial ("J. Smith")."""
    # only the word RIGHT BEFORE the full stop counts: "(e.g." yes, but not the "p" in "(p < 0.001)."
    match = re.search(r"([A-Za-z]+(?:\.[A-Za-z]+)*)\.?$", before)
    last_word = match.group(1).lower() if match else ""
    return last_word in _ABBREVIATIONS or (len(last_word) == 1 and last_word.isalpha())


def split_sentences(abstract: str) -> list[Sentence]:
    """All sentences of `abstract`, in order, each with its section label and exact offsets."""
    sentences: list[Sentence] = []
    section: str | None = None  # a line without a label continues the previous section's label

    line_start = 0
    for line in abstract.split("\n"):
        body_start = line_start
        match = _LABEL.match(line)
        if match and _is_label(match.group(1)):
            section = match.group(1).strip()
            body_start = line_start + match.end()
        _add_sentences(abstract, body_start, line_start + len(line), section, sentences)
        line_start += len(line) + 1  # +1 for the "\n"
    return sentences


def _add_sentences(abstract: str, start: int, end: int, section: str | None, out: list[Sentence]) -> None:
    """Split abstract[start:end] into sentences and append them to `out`."""
    piece_start = start
    for boundary in _BOUNDARY.finditer(abstract, start, end):
        if _is_abbreviation_end(abstract[piece_start : boundary.start()]):
            continue  # not a real sentence end, keep growing the current sentence
        _append(abstract, piece_start, boundary.start(), section, out)
        piece_start = boundary.end()
    _append(abstract, piece_start, end, section, out)


def _append(abstract: str, start: int, end: int, section: str | None, out: list[Sentence]) -> None:
    raw = abstract[start:end]
    text = raw.strip()
    if not text:
        return
    offset = start + (len(raw) - len(raw.lstrip()))  # skip leading spaces so offsets point at the text
    out.append(Sentence(text=text, start=offset, end=offset + len(text), section=section))
