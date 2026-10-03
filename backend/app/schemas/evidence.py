"""Structured evidence extracted from one paper.

The contract this file defines is what later slices (study comparison, contradiction detection,
research-gap analysis, cited synthesis) will read. Its rules:

1. An `Evidence` always names the paper it came from (`paper_id`).
2. Every field says HOW we know it (`FieldStatus`):
     STATED       - the value is written in the paper's abstract.
     INFERRED     - not in the abstract, but derivable from the paper's metadata (its title).
     UNAVAILABLE  - we could not establish it. This means "not found", NEVER "the paper has none":
                    an unavailable `comparator` does not mean "there was no comparator".
3. A stated/inferred value must cite the exact text it came from (`SourceSpan`). An unavailable
   field must be completely empty. Validators enforce this, so a half-filled or uncited value
   cannot exist.
4. Nothing is guessed: if the source text does not say it, the field stays UNAVAILABLE.
"""

from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, model_validator


class FieldStatus(str, Enum):
    STATED = "stated"
    INFERRED = "inferred"
    UNAVAILABLE = "unavailable"


# A fixed vocabulary, so later code can compare designs without parsing free text.
StudyDesign = Literal[
    "randomized controlled trial",
    "quasi-experimental study",
    "systematic review",
    "meta-analysis",
    "systematic review with meta-analysis",
    "umbrella review",
    "scoping review",
    "narrative review",
    "cohort study",
    "case-control study",
    "cross-sectional study",
    "case series",
    "case report",
    "qualitative study",
    "study protocol",
]


class SourceSpan(BaseModel):
    """The exact piece of source text a value was taken from."""

    origin: Literal["abstract", "title"]  # which Paper field the text is from
    text: str = Field(min_length=1)  # verbatim text (a sentence of the abstract, or the title)
    section: str | None = None  # abstract section label ("METHODS") if the abstract had labels
    # Character offsets into Paper.abstract / Paper.title, so that paper_text[start:end] == text.
    # Both are set or both are None (None = an extractor that cannot tell where the text was).
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _offsets_are_consistent(self) -> "SourceSpan":
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be given together")
        if self.start is not None and self.end - self.start != len(self.text):
            raise ValueError("offsets do not match the length of the text")
        return self


class _EvidenceField(BaseModel):
    """Shared rules for every extracted field (see the module docstring, rules 2 and 3)."""

    status: FieldStatus = FieldStatus.UNAVAILABLE
    sources: list[SourceSpan] = Field(default_factory=list)

    # --- subclasses say what "has a value" means and where their citations live ---
    def _has_value(self) -> bool:
        raise NotImplementedError

    def _cited_spans(self) -> list[SourceSpan]:
        return self.sources

    @model_validator(mode="after")
    def _status_matches_content(self) -> "_EvidenceField":
        spans = self._cited_spans()
        if self.status is FieldStatus.UNAVAILABLE:
            if self._has_value() or spans:
                raise ValueError("an unavailable field must be empty")
            return self
        if not self._has_value():
            raise ValueError(f"a {self.status.value} field must have a value")
        if not spans:
            raise ValueError(f"a {self.status.value} field must cite the text it came from")
        wanted_origin = "abstract" if self.status is FieldStatus.STATED else "title"
        if any(span.origin != wanted_origin for span in spans):
            raise ValueError(f"{self.status.value} values must come from the {wanted_origin}")
        return self


class TextField(_EvidenceField):
    value: str | None = None

    def _has_value(self) -> bool:
        return bool(self.value)


class ListField(_EvidenceField):
    values: list[str] = Field(default_factory=list)

    def _has_value(self) -> bool:
        return bool(self.values)


class StudyDesignField(_EvidenceField):
    value: StudyDesign | None = None

    def _has_value(self) -> bool:
        return self.value is not None


class SampleSizeField(_EvidenceField):
    """Counts exactly as the text states them. Each is None when not stated; none is ever summed or guessed."""

    participants: int | None = Field(default=None, ge=1)  # people studied (a review's pooled total counts too)
    studies: int | None = Field(default=None, ge=1)  # number of studies a review included

    def _has_value(self) -> bool:
        return self.participants is not None or self.studies is not None


class Finding(BaseModel):
    """One sentence of reported results or conclusions, with any statistics found in it."""

    kind: Literal["result", "conclusion", "unclassified"]  # "unclassified" = abstract had no section labels
    source: SourceSpan  # the sentence itself
    # Numbers copied verbatim from the sentence ("p < 0.001", "95% CI 0.2 to 0.8", "12.5%"). Never interpreted.
    statistics: list[str] = Field(default_factory=list)


class FindingsField(_EvidenceField):
    items: list[Finding] = Field(default_factory=list)

    def _has_value(self) -> bool:
        return bool(self.items)

    def _cited_spans(self) -> list[SourceSpan]:
        return [finding.source for finding in self.items]


AbstractStatus = Literal["present", "empty", "missing"]


class Evidence(BaseModel):
    """What one paper tells us, in a fixed structure. Fields we could not establish are UNAVAILABLE."""

    paper_id: str = Field(min_length=1)  # canonical Paper.id: required, so evidence is never orphaned
    extractor: str = Field(min_length=1)  # which extractor (and version) produced this

    # "missing" = the paper has no abstract (None); "empty" = it has one but it contains nothing;
    # "present" = it has text. Neither "missing" nor "empty" is evidence of anything.
    abstract_status: AbstractStatus

    study_design: StudyDesignField = Field(default_factory=StudyDesignField)
    population: TextField = Field(default_factory=TextField)
    sample_size: SampleSizeField = Field(default_factory=SampleSizeField)
    intervention: TextField = Field(default_factory=TextField)
    comparator: TextField = Field(default_factory=TextField)
    outcomes: ListField = Field(default_factory=ListField)
    findings: FindingsField = Field(default_factory=FindingsField)
    limitations: ListField = Field(default_factory=ListField)

    @model_validator(mode="after")
    def _paper_id_is_not_blank(self) -> "Evidence":
        if not self.paper_id.strip():
            raise ValueError("paper_id must not be blank")
        return self

    FIELD_NAMES: ClassVar[tuple[str, ...]] = (
        "study_design", "population", "sample_size", "intervention", "comparator",
        "outcomes", "findings", "limitations",
    )

    def available_fields(self) -> list[str]:
        return [n for n in self.FIELD_NAMES if getattr(self, n).status is not FieldStatus.UNAVAILABLE]

    def unavailable_fields(self) -> list[str]:
        return [n for n in self.FIELD_NAMES if getattr(self, n).status is FieldStatus.UNAVAILABLE]
