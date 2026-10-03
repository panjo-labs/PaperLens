"""Rule-based (no model) evidence extractor.

Principle: be CONSERVATIVE. Each rule looks for an explicit phrase; if the abstract does not clearly
say something, the field stays UNAVAILABLE. A missed value is acceptable; an invented or wrong
value is not. Rules are ordered from most to least reliable, and the first match wins.

Where a value comes from decides its status:
  * written in the abstract                 -> STATED
  * only in the title (study design, population) -> INFERRED
  * nowhere                                 -> UNAVAILABLE

Each value cites the sentence it was taken from, with real character offsets (see sections.py).
Values are copied verbatim from the text, never paraphrased.
"""

import re
from collections.abc import Callable, Sequence

from app.schemas.evidence import (
    Evidence,
    FieldStatus,
    Finding,
    FindingsField,
    ListField,
    SampleSizeField,
    SourceSpan,
    StudyDesignField,
    TextField,
)
from app.schemas.paper import Paper
from app.services.evidence.sections import Sentence, split_sentences

EXTRACTOR_NAME = "rule-based-v1"
_I = re.IGNORECASE


# ---------------------------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------------------------


def _kind(section: str | None) -> str | None:
    """Coarse meaning of an abstract section label: background / results / conclusion / other (None = unlabelled)."""
    if not section:
        return None
    label = section.upper()
    if "CONCLUSION" in label or label in {"INTERPRETATION", "IMPLICATIONS"}:
        return "conclusion"
    if label.startswith(("BACKGROUND", "INTRODUCTION", "RATIONALE", "CONTEXT", "IMPORTANCE")):
        return "background"
    if "RESULT" in label or "FINDINGS" in label:
        return "results"
    return "other"


def _label_in(sentence: Sentence, names: set[str]) -> bool:
    return bool(sentence.section) and sentence.section.upper() in names


def _abstract_span(s: Sentence) -> SourceSpan:
    return SourceSpan(origin="abstract", text=s.text, section=s.section, start=s.start, end=s.end)


def _title_span(title: str) -> SourceSpan:
    return SourceSpan(origin="title", text=title, start=0, end=len(title))


_TRAILING_CONNECTOR = re.compile(r"\s+(?:and|or|with|to|of|the|a|an|in|on|for|by|than|as)$", _I)

# Phrases too vague to count as an intervention / comparator / population on their own.
_TOO_GENERIC = frozenset(
    {"participants", "patients", "patient", "participant", "groups", "group", "controls", "control", "subjects", "people",
     "individuals", "them", "it", "those", "other", "others", "usual", "standard", "both", "each", "all", "same"}
)


def _clean(phrase: str) -> str:
    """Tidy a captured phrase: collapse spaces, drop leading articles, stray punctuation and dangling connectors."""
    phrase = re.sub(r"\s+", " ", phrase).strip(" ,;:.-?!")
    phrase = re.sub(r"^(?:the|a|an|either|both)\s+", "", phrase, flags=_I)
    while (trimmed := _TRAILING_CONNECTOR.sub("", phrase)) != phrase:  # "exercise alone or" -> "exercise alone"
        phrase = trimmed
    return phrase


def _acceptable(phrase: str) -> bool:
    """A captured phrase must be a real, specific, reasonably short noun phrase; otherwise we say nothing."""
    return 3 <= len(phrase) <= 120 and any(c.isalpha() for c in phrase) and phrase.lower() not in _TOO_GENERIC


def _negated(text: str, position: int) -> bool:
    """True if the words just before `position` negate what follows ("no control group", "without blinding")."""
    return bool(re.search(r"\b(?:no|not|without|lack(?:ing)?(?: of)?|absence of)\s+(?:\w+\s+){0,2}$", text[max(0, position - 30) : position], _I))


# ---------------------------------------------------------------------------------------------
# Study design
# ---------------------------------------------------------------------------------------------

# Words that show a sentence is talking about OTHER studies, not describing this one.
_ABOUT_OTHER_STUDIES = re.compile(
    r"\b(?:previous|prior|earlier|existing|recent|published|several|many|most|few|other|available)\s+(?:\w+\s+){0,2}?"
    r"(?:systematic reviews?|meta-analys[ei]s|reviews?|trials?|studies|randomi[sz]ed|RCTs?)\b"
    r"|\bevidence (?:from|suggests?|shows?)\b|\bhave (?:shown|reported|found|demonstrated)\b",
    _I,
)

_SYSTEMATIC = re.compile(r"\bsystematic(?:ally)? (?:literature )?review|\bsystematic search", _I)
_META = re.compile(r"\bmeta-?analy(?:sis|ses|tic)\b", _I)
# Typical wording of a literature search: marks a review even if it never says "systematic review".
_SEARCHED = re.compile(
    r"\bsearch(?:ed|es)?\b[^.]{0,80}\b(?:PubMed|MEDLINE|Embase|Cochrane|CENTRAL|Scopus|Web of Science|CINAHL|PEDro|databases?)\b"
    r"|\b(?:PubMed|MEDLINE|Embase|Cochrane|CENTRAL|Scopus|CINAHL|PEDro|databases?)\b[^.]{0,80}\bwere searched\b|\bPRISMA\b",
    _I,
)

# Designs whose wording could also be mistaken for a plain review, so they are checked FIRST.
_DESIGNS_BEFORE_REVIEWS: list[tuple[str, re.Pattern[str]]] = [
    ("study protocol", re.compile(r"\b(?:study|trial|review|research) protocol\b|\bprotocol (?:for|of) (?:a|an|the)\b|\bthis protocol\b", _I)),
    ("umbrella review", re.compile(r"\bumbrella review\b", _I)),
    ("scoping review", re.compile(r"\bscoping review\b", _I)),
]

# Checked AFTER the review test, so a review that merely INCLUDES randomized trials is never
# labelled "randomized controlled trial".
_DESIGNS_AFTER_REVIEWS: list[tuple[str, re.Pattern[str]]] = [
    ("quasi-experimental study", re.compile(r"\bquasi[- ]?experimental\b|\bnon-?randomi[sz]ed\b", _I)),
    (
        "randomized controlled trial",
        re.compile(
            r"(?<!non-)(?<!non )\brandomi[sz]ed\b[^.;]{0,60}?\b(?:trial|study)\b|\bRCTs?\b"
            r"|\brandomly (?:assigned|allocated|divided)\b|(?<!non-)\bwere randomi[sz]ed\b",
            _I,
        ),
    ),
    ("cohort study", re.compile(r"\bcohort (?:study|design)\b", _I)),
    ("case-control study", re.compile(r"\bcase[- ]control\b", _I)),
    # (not "muscle fibre cross-sectional area", which is a measurement, not a design)
    ("cross-sectional study", re.compile(r"\bcross[- ]sectional\b(?!\s+(?:area|diameter|imaging|view|slice))", _I)),
    ("case series", re.compile(r"\bcase series\b", _I)),
    ("case report", re.compile(r"\bcase report\b", _I)),
    ("qualitative study", re.compile(r"\bqualitative (?:study|research|interviews?)\b", _I)),
    ("narrative review", re.compile(r"\bnarrative review\b|\bliterature review\b", _I)),
]

# Sections that describe a review's search; their presence alone shows the paper is a review.
_REVIEW_SECTIONS = ("SEARCH", "SELECTION CRITERIA", "DATA SOURCES", "STUDY SELECTION", "DATA COLLECTION", "REVIEW METHODS")

Candidates = Sequence[tuple[str, SourceSpan]]  # [(text, span that cites it), ...]


def _first_match(candidates: Candidates, pattern: re.Pattern[str]) -> SourceSpan | None:
    return next((span for text, span in candidates if pattern.search(text)), None)


def _detect_review(candidates: Candidates, sections: Sequence[str | None]) -> tuple[str, list[SourceSpan]] | None:
    """Systematic review / meta-analysis, judged from wording about searching or pooling studies."""
    systematic = _first_match(candidates, _SYSTEMATIC) or _first_match(candidates, _SEARCHED)
    if systematic is None and candidates and any((s or "").upper().startswith(_REVIEW_SECTIONS) for s in sections):
        systematic = candidates[0][1]  # a "SEARCH METHODS" section can only belong to a review
    meta = _first_match(candidates, _META)
    if systematic and meta:
        return "systematic review with meta-analysis", [systematic] if systematic is meta else [systematic, meta]
    if systematic:
        return "systematic review", [systematic]
    if meta:
        return "meta-analysis", [meta]
    return None


def _detect_design(candidates: Candidates, sections: Sequence[str | None] = ()) -> tuple[str, list[SourceSpan]] | None:
    """Find the study design in `candidates`. Returns (label, citing spans) or None."""
    for label, pattern in _DESIGNS_BEFORE_REVIEWS:
        if span := _first_match(candidates, pattern):
            return label, [span]
    if review := _detect_review(candidates, sections):
        return review
    for label, pattern in _DESIGNS_AFTER_REVIEWS:
        if span := _first_match(candidates, pattern):
            return label, [span]
    return None


def _extract_study_design(paper: Paper, sentences: list[Sentence]) -> StudyDesignField:
    # 1) the abstract, skipping conclusions, general background and sentences about OTHER studies
    own = [
        (s.text, _abstract_span(s))
        for s in sentences
        if _kind(s.section) != "conclusion"
        and (_kind(s.section) != "background" or _AIM_OR_METHOD.search(s.text))
        and not _ABOUT_OTHER_STUDIES.search(s.text)
    ]
    from_title = _detect_design([(paper.title, _title_span(paper.title))])  # only ever INFERRED
    found = _detect_design(own, [s.section for s in sentences])
    if found:
        label, spans = found
        # "databases were searched" also appears in narrative reviews. If that is all the abstract says for a
        # review and the title names a DIFFERENT review type outright, the title is the better evidence.
        weak_review_cue = label.endswith(("review", "meta-analysis")) and not any(
            _SYSTEMATIC.search(span.text) or _META.search(span.text) for span in spans
        )
        if not (weak_review_cue and from_title and from_title[0] != label):
            return StudyDesignField(status=FieldStatus.STATED, value=label, sources=spans)
    if from_title:
        return StudyDesignField(status=FieldStatus.INFERRED, value=from_title[0], sources=from_title[1])
    return StudyDesignField()


# ---------------------------------------------------------------------------------------------
# Population, intervention, comparator, outcomes (a labelled section beats a sentence pattern)
# ---------------------------------------------------------------------------------------------

_POP_WORDS = (
    r"(?:patients|participants|adults|adolescents|children|infants|people|persons|individuals|women|men|athletes|students"
    r"|workers|veterans|volunteers|subjects|players|nurses|older adults|elderly|residents|survivors|mothers|teachers|employees)"
)
_POP_TAIL = r"(?:\s+(?:with|without|undergoing|who|aged|after|following|at risk of|living with|diagnosed with|having|suffering from)\s+[^,.;:()]{2,70}?)?"
_POP_END = r"(?=\s+(?:on|for|compared|versus|vs\.?|using|as|to|by|were|was|is|are|at|during|receiving|in)\b|\s*[,.;:()]|$)"
_POPULATION_PATTERNS = [
    # "in/among/of adults with chronic low back pain"
    re.compile(r"\b(?:in|among|of|for)\s+(?P<x>(?:(?!\d)[\w'-]+\s+){0,3}?" + _POP_WORDS + _POP_TAIL + r")" + _POP_END, _I),
    # "120 patients with knee osteoarthritis were ..."
    re.compile(r"\b\d[\d,]*\s+(?P<x>(?:[\w'-]+\s+){0,2}?" + _POP_WORDS + r"\s+(?:with|aged|who)\s+[^,.;:()]{2,70}?)" + _POP_END, _I),
    # "44 college students were ..." (no condition given)
    re.compile(r"\b\d[\d,]*\s+(?P<x>(?:[\w'-]+\s+){0,2}?" + _POP_WORDS + r")" + _POP_END, _I),
]
_POPULATION_SECTIONS = {"PARTICIPANTS", "POPULATION", "PATIENTS", "SUBJECTS", "SAMPLE", "STUDY POPULATION", "SETTING AND PARTICIPANTS", "PATIENTS/PARTICIPANTS"}

_INTERVENTION_SECTIONS = {"INTERVENTION", "INTERVENTIONS", "EXPOSURE", "EXPOSURES", "TREATMENT", "TREATMENTS"}
_INTERVENTION_PATTERNS = [
    # "effect(s) / effectiveness / efficacy of X on|in|for|compared ..."
    re.compile(r"\b(?:effects?|effectiveness|efficacy|impacts?|benefits?)\s+of\s+(?P<x>[^;.():]+?)(?=\s+(?:on|in(?!\s+(?:combination|addition))|for|among|compared|versus|vs\.?|at|during|after|following)\b|\s*[;.():]|$)", _I),
    # "participants were randomized to X (or|versus) ..."
    re.compile(r"\brandomi[sz]ed\s+(?:to|into)\s+(?:receive\s+)?(?P<x>[^,;.():]+?)(?=\s+(?:or|versus|vs\.?|and)\b|\s*[,;.():]|$)", _I),
]

_COMPARATOR_SECTIONS = {"COMPARISON", "COMPARISONS", "COMPARATOR", "COMPARATORS", "CONTROL", "CONTROLS", "COMPARISON GROUP"}
_COMPARATOR_PATTERNS = [
    # "... compared with / versus / vs. X"
    re.compile(r"\b(?:compared (?:with|to)|in comparison (?:with|to)|versus|vs\.?)\s+(?P<x>[^,;.():]+?)(?=\s+(?:on|in(?!\s+(?:combination|addition))|for|among|at|after|during|following|using|regarding|by|with|was|were|is|are|had|has|did)\b|\s*[,;.():]|$)", _I),
    # "a control group", "sham group", "placebo arm"
    re.compile(r"\b(?P<x>(?:control|comparison|placebo|sham|usual[- ]care|standard[- ]care|waiting[- ]list)\s+(?:group|arm|condition))\b", _I),
]

_OUTCOME_SECTIONS = {"OUTCOME", "OUTCOMES", "MAIN OUTCOME MEASURE", "MAIN OUTCOME MEASURES", "PRIMARY OUTCOME", "PRIMARY OUTCOMES", "OUTCOME MEASURES", "MEASUREMENTS", "MAIN OUTCOME MEASUREMENTS", "MEASURES", "MAIN OUTCOMES AND MEASURES", "MAIN OUTCOMES", "OUTCOME MEASURE"}
_OUTCOME_PATTERNS = [
    # "primary outcomes were X, Y and Z"
    re.compile(r"\b(?:primary|main|secondary|key)?\s*outcomes?(?:\s+(?:measures?|variables?))?\s+(?:was|were|included?|consisted of)\s+(?P<x>[^.;]+)", _I),
    # "effects of X on pain, function and quality of life in ..."
    re.compile(r"\b(?:effects?|effectiveness|efficacy|impacts?|influence)\s+of\s+[^.;]+?\s+on\s+(?P<x>[^.;]+?)(?=\s+(?:in|among|compared|versus|vs\.?|after|during|following)\b|\s*[.;]|$)", _I),
    # "outcome data relating to pain, function and mouth opening were extracted"
    re.compile(r"\boutcomes?(?:\s+data)?\s+(?:relating|related)\s+to\s+(?P<x>[^.;]+?)(?=\s+(?:were|was)\b|\s*[.;]|$)", _I),
]


def _first_phrase(patterns, sentences: Sequence[Sentence]) -> tuple[str, Sentence] | None:
    """The first acceptable phrase any pattern captures, scanning patterns in order, then sentences in order."""
    for pattern in patterns:
        for s in sentences:
            for match in pattern.finditer(s.text):
                phrase = _clean(match.group("x"))
                if _acceptable(phrase) and not _negated(s.text, match.start()):
                    return phrase, s
    return None


# a person word in any form: an "outcome" containing one is really describing who was studied
_PERSON_WORD = re.compile(r"\b(?:patient|participant|adult|adolescent|child|infant|person|people|individual|wom[ae]n|m[ae]n|athlete|student|worker|veteran|volunteer|subject|player|nurse|elderly|resident|survivor)s?\b", _I)
# "worker with SIS", "patients who ...": a description of the people, not an outcome. ("student performance" is fine.)
_DESCRIBES_PEOPLE = re.compile(_PERSON_WORD.pattern + r"\s+(?:with|who|aged|suffering|undergoing)\b", _I)
_ARTICLE_START = re.compile(r"^(?:a|an|the)\s", _I)
_TRAILING_TIMING = re.compile(r"\s+\d+\s*(?:weeks?|months?|days?|years?|hours?)$", _I)
_STARTS_WITH_TIME = re.compile(r"^\d+\s*(?:weeks?|months?|days?|years?|hours?)\b", _I)  # "12months postrandomisation"
_STARTS_WITH_PREPOSITION = re.compile(r"^(?:using|via|with|by|through|during|after|before|following|between|within|at)\b", _I)
_MODAL = re.compile(r"\b(?:can|could|will|would|should|may|might)\b", _I)


def _split_list(phrase: str) -> list[str]:
    """Split "pain, disability and quality of life" into items, but only along commas.

    "pain and function" stays ONE item: splitting on every "and" breaks phrases such as
    "physiological and cognitive responses" or "Western Ontario and McMaster Universities Index", and a
    verbatim phrase is safer than a wrong split. Only the last "and"/"or" of a comma list is split.
    Items that look like a person, a time frame or a sentence fragment are dropped: they are not outcomes
    ("a part-time worker with SIS" is who was studied).
    """
    parts = re.split(r"\s*[,;]\s*(?![^()]*\))", phrase)  # (a comma inside parentheses is not a separator)
    if len(parts) > 1:
        last = parts[-1]
        if match := re.match(r"^(?:and|or)\s+(.*)$", last, _I):  # "A, B, and C"
            parts[-1] = match.group(1)
        elif match := re.match(r"^(.*?)\s+(?:and|or)\s+([a-z].*)$", last):  # "A, B and c" (next word lower-case)
            parts[-1:] = [match.group(1), match.group(2)]

    items = []
    for raw in parts:
        if _ARTICLE_START.match(raw.strip()):
            continue
        item = _TRAILING_TIMING.sub("", _clean(raw))  # "fibre size 2 weeks" -> "fibre size"
        if (
            _acceptable(item)
            and len(item.split()) <= 25
            and not _DESCRIBES_PEOPLE.search(item)
            and not _MODAL.search(item)
            and not _STARTS_WITH_TIME.match(item)
            and not _STARTS_WITH_PREPOSITION.match(item)
        ):
            items.append(item)
    return list(dict.fromkeys(items))


def _text_field(sentences: list[Sentence], section_names: set[str], patterns, title: str | None = None, title_patterns=()) -> TextField:
    # 1) a labelled section ("PARTICIPANTS: ...") is the most reliable source
    labelled = next((s for s in sentences if _label_in(s, section_names)), None)
    if labelled:
        return TextField(status=FieldStatus.STATED, value=labelled.text, sources=[_abstract_span(labelled)])
    # 2) a sentence pattern
    found = _first_phrase(patterns, sentences)
    if found:
        return TextField(status=FieldStatus.STATED, value=found[0], sources=[_abstract_span(found[1])])
    # 3) the title (only for fields where we allow it): INFERRED
    if title and title_patterns:
        title_sentence = Sentence(text=title, start=0, end=len(title), section=None)
        found = _first_phrase(title_patterns, [title_sentence])
        if found:
            return TextField(status=FieldStatus.INFERRED, value=found[0], sources=[_title_span(title)])
    return TextField()


# Sentences that state an aim or describe the method (as opposed to reporting results).
_AIM_OR_METHOD = re.compile(
    r"\b(?:aims?|aimed|purposes?|objectives?|sought|goals?|randomi[sz]ed|randomly|allocated|assigned|divided into|received|"
    r"studied|investigated|examined|evaluated|assessed|explored|tested|measured|recruited|enrolled|"
    r"were (?:given|treated|recruited|enrolled)|undertook|conducted|"
    r"to (?:investigate|evaluate|assess|examine|determine|compare|test|explore|study|analy[sz]e|identify|describe))\b",
    _I,
)


def _design_level_sentences(sentences: list[Sentence]) -> list[Sentence]:
    """Sentences that may state who was studied, what was done and what was measured.

    Labelled abstract: not RESULTS or CONCLUSION; a BACKGROUND sentence only if it states the aim
    ("This study aimed to...") because the rest of the background is general context about the field.
    Unlabelled abstract: only sentences that state an aim or describe the method, because without labels
    a results sentence ("... improved more than the control group") looks like any other.
    """
    if any(s.section for s in sentences):
        return [
            s for s in sentences
            if _kind(s.section) not in ("results", "conclusion")
            and (_kind(s.section) != "background" or _AIM_OR_METHOD.search(s.text))
        ]
    return [s for s in sentences if _AIM_OR_METHOD.search(s.text)]


def _extract_outcomes(sentences: list[Sentence]) -> ListField:
    labelled = next((s for s in sentences if _label_in(s, _OUTCOME_SECTIONS)), None)
    if labelled:
        values = _split_list(labelled.text)
        if values:
            return ListField(status=FieldStatus.STATED, values=values, sources=[_abstract_span(labelled)])
    found = _first_phrase(_OUTCOME_PATTERNS, sentences)
    if found:
        values = _split_list(found[0])
        if values:
            return ListField(status=FieldStatus.STATED, values=values, sources=[_abstract_span(found[1])])
    return ListField()


# ---------------------------------------------------------------------------------------------
# Sample size
# ---------------------------------------------------------------------------------------------

_PEOPLE = r"(?:participants|patients|subjects|individuals|adults|people|volunteers|women|men|children|adolescents|students|athletes|players|respondents|workers|nurses|older adults)"
_N_EQUALS = re.compile(r"\bn\s*=\s*(?P<n>\d[\d,]*)", _I)
_N_PEOPLE = re.compile(r"\b(?P<n>\d{1,3}(?:,\d{3})+|\d+)\s+(?!(?:million|billion|thousand)\b)(?:[\w'-]+\s+){0,2}?" + _PEOPLE + r"\b", _I)
# A sentence has to talk about recruiting / studying people before a number in it can be the sample size.
_SAMPLE_CUE = re.compile(
    r"\b(?:included|includes|enrolled|recruited|randomi[sz]ed|randomly|allocated|assigned|participated|sample|total|studied|tested|examined|evaluated|analy[sz]ed|n\s*=)\b",
    _I,
)
# "107 participants COMPLETED...", "1582 individuals were SCREENED": counts of a subset or of a step, not the sample.
_SUBSET = re.compile(r"^\s*(?:\w+\s+){0,2}?(?:completed|screened|assessed|qualified|dropped|finished|excluded|withdrew|lost|interested|eligible|declined|refused)\b", _I)
_PER_GROUP = re.compile(r"^\s*(?:\w+\s+){0,3}?(?:per|in each|each)\s+(?:group|arm)\b|^\s*each\b", _I)
_ENROLMENT = re.compile(r"\btotal of\b|\bin total\b|\boverall\b|\b(?:were|was)\s+(?:randomi[sz]ed|randomly|enrolled|recruited|included|analy[sz]ed|assigned|allocated)\b|\b(?:enrolled|recruited|randomi[sz]ed)\b", _I)
_N_STUDIES = re.compile(r"\b(?P<n>\d+)\s+(?:[\w'-]+\s+){0,3}?(?:studies|trials|RCTs|articles|publications|papers|reports)\b", _I)
_INCLUDED = re.compile(r"\b(?:included|identified|met|eligible|comprising|comprised|retrieved|selected|reviewed)\b", _I)


def _to_int(digits: str) -> int:
    return int(digits.replace(",", ""))


_UNIT_WORDS ={"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEEN_WORDS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_UNIT = "|".join(_UNIT_WORDS)
_BELOW_HUNDRED = "(?:(?:" + "|".join(_TENS_WORDS) + ")(?:[- ](?:" + _UNIT + "))?|" + "|".join(_TEEN_WORDS) + ")"
# "Seventy-five", "Thirteen", "One hundred and forty-five". A lone "one".."nine" is NOT converted ("no one", "one of the groups").
_NUMBER_WORDS = re.compile(
    r"\b(?:(?P<h>" + _UNIT + r")\s+hundred(?:\s+(?:and\s+)?(?P<r>" + _BELOW_HUNDRED + "|" + _UNIT + r"))?|(?P<s>" + _BELOW_HUNDRED + r"))\b",
    _I,
)


def _words_to_int(words: str) -> int:
    return sum({**_UNIT_WORDS, **_TEEN_WORDS, **_TENS_WORDS}[w.lower()] for w in re.split(r"[- ]", words) if w)


def _numbers_as_digits(text: str) -> str:
    """"Seventy-five adults" -> "75 adults". Used only for counting; the cited sentence is never altered."""

    def convert(match: re.Match[str]) -> str:
        if match.group("s"):
            return str(_words_to_int(match.group("s")))
        return str(100 * _UNIT_WORDS[match.group("h").lower()] + (_words_to_int(match.group("r")) if match.group("r") else 0))

    return _NUMBER_WORDS.sub(convert, text)


def _pick(candidates: list[tuple[int, Sentence]]) -> tuple[int, Sentence] | None:
    """ONE number if the text is unambiguous, else None ("unavailable" beats guessing)."""
    if len(candidates) == 1:
        return candidates[0]
    # Several numbers: only trust one marked as the total / the enrolment count.
    totals = [(n, s) for n, s in candidates if _ENROLMENT.search(s.text)]
    return totals[0] if len(totals) == 1 else None


def _count(sentences: list[Sentence], matchers: list[Callable[[Sentence], list[int]]]) -> tuple[int, Sentence] | None:
    """Try each matcher in order of trust. A matcher that finds numbers but cannot decide between them stops the search."""
    for matcher in matchers:
        # a count of 0 ("0 studies met the criteria") is a finding, not a sample size
        candidates = [(n, s) for s in sentences for n in matcher(s) if n >= 1]
        if candidates:
            return _pick(candidates)
    return None


def _extract_sample_size(sentences: list[Sentence]) -> SampleSizeField:
    usable = [s for s in sentences if _kind(s.section) not in ("background", "conclusion")]

    def regex_counts(pattern: re.Pattern[str]) -> Callable[[Sentence], list[int]]:
        def find(s: Sentence) -> list[int]:
            if not _SAMPLE_CUE.search(s.text):  # "288 people interested in participating" says nothing about the sample
                return []
            text = _numbers_as_digits(s.text)
            return [
                _to_int(m.group("n"))
                for m in pattern.finditer(text)
                # not "30 patients per group" (a group size) nor "107 participants completed ..." (a subset)
                if not _PER_GROUP.match(text[m.end() :]) and not _SUBSET.match(text[m.end() :])
            ]

        return find

    def studies(s: Sentence) -> list[int]:
        if not _INCLUDED.search(s.text):
            return []
        return [_to_int(m.group("n")) for m in _N_STUDIES.finditer(_numbers_as_digits(s.text))]

    # "34 patients" is trusted before "(n = 17)": the latter is usually a group size.
    people_found = _count(usable, [regex_counts(_N_PEOPLE), regex_counts(_N_EQUALS)])
    studies_found = _count(usable, [studies])
    if not people_found and not studies_found:
        return SampleSizeField()
    spans: dict[int, SourceSpan] = {}  # keyed by offset so a sentence used twice is cited once
    for found in (people_found, studies_found):
        if found:
            spans[found[1].start] = _abstract_span(found[1])
    return SampleSizeField(
        status=FieldStatus.STATED,
        participants=people_found[0] if people_found else None,
        studies=studies_found[0] if studies_found else None,
        sources=list(spans.values()),
    )

# ---------------------------------------------------------------------------------------------
# Findings (with numbers copied verbatim) and limitations
# ---------------------------------------------------------------------------------------------

_STATISTICS = [
    re.compile(r"\b[pP]\s*(?:<=|>=|[<>=≤≥])\s*(?:\d*[.·])?\d+(?:[eE]-?\d+)?"),  # p < 0.001, P = .03
    re.compile(r"\b95\s?%\s*(?:CI|confidence interval)(?:\s*\[CI\])?\s*[:=,]?\s*(?:\(\s*-?\d+(?:[.·]\d+)?\s*(?:to|-|–|,|;)\s*-?\d+(?:[.·]\d+)?\s*\)|-?\d+(?:[.·]\d+)?\s*(?:to|-|–|,|;)\s*-?\d+(?:[.·]\d+)?)", _I),  # 95% CI 0.2 to 0.8
    re.compile(r"\b(?:SMD|WMD|MD|OR|RR|HR|IRR|AOR)\s*[=:,]?\s*-?\d+(?:\.\d+)?"),  # SMD -0.5, OR 1.8
    re.compile(r"(?:\bCohen'?s\s+d|\bHedges'?\s+g|\b[dgr])\s*=\s*-?\d*\.?\d+"),  # d = 0.8, r = 0.45
    re.compile(r"-?\d+(?:\.\d+)?\s?%"),  # 12.5%
]
_FINDING_CUE = re.compile(
    r"\b(?:significant(?:ly)?|improv\w+|reduc\w+|decreas\w+|increas\w+|differ\w*|associated|effective|ineffective|superior|inferior|"
    r"better|greater|lower|higher|no (?:significant )?(?:difference|effect|improvement)|did not)\b",
    _I,
)
_STATES_AN_AIM = re.compile(r"^\s*(?:to\b|the (?:aim|purpose|objective)|this (?:study|trial|review) (?:aimed|sought|investigated|examined|evaluated|compared))", _I)

_LIMITATION_CUE = re.compile(
    r"\b(?:main|major|key|primary|important|one|another|several|study|our|present|current|its)\s+(?:\w+\s+)?limitations?\b"  # "a main limitation"
    r"|\blimitations?\s+(?:of\s+(?:this|the|our|present|current)|include[ds]?|were|was|are|is)\b"  # "limitations of this study"
    r"|\blimited by\b|\bsmall sample(?: size)?\b|\bshort (?:follow-?up|duration)\b"
    r"|\black of (?:a )?(?:blinding|randomi[sz]ation|control group|follow-?up|power)\b|\b(?:not|without) blind(?:ed|ing)\b"
    r"|\binterpret(?:ed)? (?:with caution|cautiously)\b|\b(?:very )?low (?:certainty|quality) (?:of )?evidence\b",
    _I,
)


def statistics_in(text: str) -> list[str]:
    """Numbers/statistics in `text`, copied verbatim, in order of appearance, without repeats.

    A match that lies inside a longer match is dropped: the "95%" inside "95% CI -2 to -0.2" is not a
    separate statistic.
    """
    spans = sorted(
        {(m.start(), m.end()) for pattern in _STATISTICS for m in pattern.finditer(text)},
        key=lambda span: (span[0], -(span[1] - span[0])),  # earliest first; the longest first when they start together
    )
    kept: list[tuple[int, int]] = []
    for start, end in spans:
        if not any(k_start <= start and end <= k_end for k_start, k_end in kept):
            kept.append((start, end))
    return list(dict.fromkeys(text[start:end].strip() for start, end in kept))


def _extract_findings(sentences: list[Sentence]) -> FindingsField:
    labelled = any(s.section for s in sentences)
    items: list[Finding] = []
    for s in sentences:
        kind = _kind(s.section)
        if labelled:
            if kind not in ("results", "conclusion"):
                continue
            label = "result" if kind == "results" else "conclusion"
        else:
            # No section labels: keep only sentences that look like reported results.
            if _STATES_AN_AIM.search(s.text) or not (statistics_in(s.text) or _FINDING_CUE.search(s.text)):
                continue
            label = "unclassified"
        items.append(Finding(kind=label, source=_abstract_span(s), statistics=statistics_in(s.text)))
    return FindingsField(status=FieldStatus.STATED, items=items) if items else FindingsField()


def _extract_limitations(sentences: list[Sentence]) -> ListField:
    hits = []
    for s in sentences:
        if _kind(s.section) == "background":
            continue
        cue = _LIMITATION_CUE.search(s.text)
        if (cue and not _negated(s.text, cue.start())) or _label_in(s, {"LIMITATION", "LIMITATIONS"}):
            hits.append(s)
    if not hits:
        return ListField()
    return ListField(status=FieldStatus.STATED, values=[s.text for s in hits], sources=[_abstract_span(s) for s in hits])


# ---------------------------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------------------------


def _abstract_status(abstract: str | None) -> str:
    if abstract is None:
        return "missing"  # the provider gave no abstract
    return "present" if abstract.strip() else "empty"  # "" is different from None: it was supplied but holds nothing


class RuleBasedEvidenceExtractor:
    name = EXTRACTOR_NAME

    async def extract(self, paper: Paper) -> Evidence:
        return self.extract_sync(paper)

    def extract_sync(self, paper: Paper) -> Evidence:
        """The actual work (pure CPU, no I/O); `extract` just exposes it through the async interface."""
        status = _abstract_status(paper.abstract)
        sentences = split_sentences(paper.abstract) if status == "present" else []
        design_sentences = _design_level_sentences(sentences)

        return Evidence(
            paper_id=paper.id,
            extractor=self.name,
            abstract_status=status,
            study_design=_extract_study_design(paper, sentences),
            population=_text_field(
                design_sentences, _POPULATION_SECTIONS, _POPULATION_PATTERNS,
                title=paper.title, title_patterns=_POPULATION_PATTERNS,
            ),
            sample_size=_extract_sample_size(sentences),
            intervention=_text_field(design_sentences, _INTERVENTION_SECTIONS, _INTERVENTION_PATTERNS),
            comparator=_text_field(design_sentences, _COMPARATOR_SECTIONS, _COMPARATOR_PATTERNS),
            outcomes=_extract_outcomes(design_sentences),
            findings=_extract_findings(sentences),
            limitations=_extract_limitations(sentences),
        )
