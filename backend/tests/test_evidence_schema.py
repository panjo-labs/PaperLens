"""The evidence contract: what an Evidence object may and may not look like."""

import pytest
from pydantic import ValidationError

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


def abstract_span(text="We studied adults.", start=0, section="METHODS") -> SourceSpan:
    return SourceSpan(origin="abstract", text=text, section=section, start=start, end=start + len(text))


def title_span(text="A randomized controlled trial") -> SourceSpan:
    return SourceSpan(origin="title", text=text, start=0, end=len(text))


# --- an evidence record always belongs to a paper -------------------------------------------------


def test_evidence_cannot_exist_without_a_paper_id():
    with pytest.raises(ValidationError):
        Evidence(extractor="x", abstract_status="present")


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_blank_paper_id_is_rejected(bad_id):
    with pytest.raises(ValidationError):
        Evidence(paper_id=bad_id, extractor="x", abstract_status="present")


def test_extractor_name_and_abstract_status_are_required_and_checked():
    with pytest.raises(ValidationError):
        Evidence(paper_id="p:1", abstract_status="present")
    with pytest.raises(ValidationError):
        Evidence(paper_id="p:1", extractor="x")
    with pytest.raises(ValidationError):
        Evidence(paper_id="p:1", extractor="x", abstract_status="unknown")


@pytest.mark.parametrize("status", ["present", "empty", "missing"])
def test_all_three_abstract_states_are_distinct_valid_values(status):
    assert Evidence(paper_id="p:1", extractor="x", abstract_status=status).abstract_status == status


def test_a_new_evidence_has_every_field_unavailable():
    ev = Evidence(paper_id="p:1", extractor="x", abstract_status="missing")

    assert ev.available_fields() == []
    assert ev.unavailable_fields() == list(Evidence.FIELD_NAMES)
    assert ev.study_design.value is None and ev.population.value is None
    assert ev.sample_size.participants is None and ev.sample_size.studies is None
    assert ev.outcomes.values == [] and ev.findings.items == []


# --- the status/content rules ---------------------------------------------------------------------


def test_stated_value_must_cite_its_source_text():
    with pytest.raises(ValidationError, match="cite"):
        TextField(status=FieldStatus.STATED, value="adults")


def test_stated_field_needs_a_value():
    with pytest.raises(ValidationError, match="value"):
        TextField(status=FieldStatus.STATED, sources=[abstract_span()])


def test_unavailable_field_must_be_completely_empty():
    with pytest.raises(ValidationError, match="empty"):
        TextField(status=FieldStatus.UNAVAILABLE, value="adults")
    with pytest.raises(ValidationError, match="empty"):
        TextField(status=FieldStatus.UNAVAILABLE, sources=[abstract_span()])
    with pytest.raises(ValidationError, match="empty"):
        ListField(status=FieldStatus.UNAVAILABLE, values=["pain"])
    with pytest.raises(ValidationError, match="empty"):
        SampleSizeField(status=FieldStatus.UNAVAILABLE, participants=10)


def test_a_field_with_a_value_cannot_be_left_marked_unavailable():
    # the default status is UNAVAILABLE, so forgetting to set the status is caught
    with pytest.raises(ValidationError):
        TextField(value="adults", sources=[abstract_span()])


def test_stated_must_come_from_the_abstract_and_inferred_only_from_the_title():
    with pytest.raises(ValidationError, match="abstract"):
        TextField(status=FieldStatus.STATED, value="x", sources=[title_span()])
    with pytest.raises(ValidationError, match="title"):
        TextField(status=FieldStatus.INFERRED, value="x", sources=[abstract_span()])
    assert TextField(status=FieldStatus.INFERRED, value="x", sources=[title_span()]).status is FieldStatus.INFERRED


def test_every_field_type_obeys_the_same_rules():
    span = abstract_span()
    ok = [
        ListField(status=FieldStatus.STATED, values=["pain"], sources=[span]),
        StudyDesignField(status=FieldStatus.STATED, value="randomized controlled trial", sources=[span]),
        SampleSizeField(status=FieldStatus.STATED, participants=40, sources=[span]),
        FindingsField(status=FieldStatus.STATED, items=[Finding(kind="result", source=span)]),
    ]
    assert all(f.status is FieldStatus.STATED for f in ok)
    with pytest.raises(ValidationError):
        ListField(status=FieldStatus.STATED, values=["pain"])
    with pytest.raises(ValidationError):
        SampleSizeField(status=FieldStatus.STATED, participants=40)
    with pytest.raises(ValidationError):
        StudyDesignField(status=FieldStatus.STATED, value="randomized controlled trial")
    with pytest.raises(ValidationError):
        FindingsField(status=FieldStatus.STATED)  # stated but no findings


def test_study_design_is_limited_to_the_fixed_vocabulary():
    with pytest.raises(ValidationError):
        StudyDesignField(status=FieldStatus.STATED, value="it was probably a trial", sources=[abstract_span()])


@pytest.mark.parametrize("kwargs", [{"participants": 0}, {"participants": -5}, {"studies": 0}])
def test_a_sample_size_must_be_a_positive_count(kwargs):
    with pytest.raises(ValidationError):
        SampleSizeField(status=FieldStatus.STATED, sources=[abstract_span()], **kwargs)


def test_findings_cite_their_own_sentences_not_a_separate_list():
    finding = Finding(kind="result", source=abstract_span("Pain fell (p < 0.05)."), statistics=["p < 0.05"])
    field = FindingsField(status=FieldStatus.STATED, items=[finding])
    assert field.sources == []  # citations live on each finding
    assert field.items[0].source.text == "Pain fell (p < 0.05)."


# --- source spans ------------------------------------------------------------------------------


def test_span_offsets_must_match_the_text_length():
    SourceSpan(origin="abstract", text="abc", start=4, end=7)
    with pytest.raises(ValidationError):
        SourceSpan(origin="abstract", text="abc", start=4, end=9)


def test_offsets_are_never_half_given_but_may_both_be_absent():
    assert SourceSpan(origin="abstract", text="abc").start is None  # unknown where: no invented numbers
    with pytest.raises(ValidationError):
        SourceSpan(origin="abstract", text="abc", start=4)
    with pytest.raises(ValidationError):
        SourceSpan(origin="abstract", text="abc", end=7)


def test_span_text_cannot_be_empty():
    with pytest.raises(ValidationError):
        SourceSpan(origin="abstract", text="")


# --- serialisation -----------------------------------------------------------------------------


def test_evidence_survives_a_json_round_trip():
    ev = Evidence(
        paper_id="p:1", extractor="x", abstract_status="present",
        study_design=StudyDesignField(status=FieldStatus.STATED, value="cohort study", sources=[abstract_span()]),
        sample_size=SampleSizeField(status=FieldStatus.STATED, participants=12, sources=[abstract_span()]),
    )
    again = Evidence.model_validate_json(ev.model_dump_json())

    assert again == ev
    assert again.available_fields() == ["study_design", "sample_size"]


def test_json_shows_status_as_plain_words():
    data = Evidence(paper_id="p:1", extractor="x", abstract_status="missing").model_dump(mode="json")
    assert data["study_design"]["status"] == "unavailable"
    assert data["abstract_status"] == "missing"
