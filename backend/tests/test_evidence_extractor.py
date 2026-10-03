"""Rule-based evidence extraction: what it finds, and (just as important) what it refuses to guess."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.schemas.evidence import FieldStatus
from app.schemas.paper import Paper
from app.services.evidence import rule_based
from app.services.evidence.rule_based import RuleBasedEvidenceExtractor, statistics_in
from tests.conftest import FIXTURES

EVAL_DIR = FIXTURES.parent.parent / "evaluation"
extractor = RuleBasedEvidenceExtractor()


def make_paper(abstract, title="A study", pid="pubmed:1") -> Paper:
    return Paper(id=pid, title=title, authors=[], abstract=abstract, source="pubmed", source_id=pid.split(":")[1])


def extract(abstract, title="A study", pid="pubmed:1"):
    return extractor.extract_sync(make_paper(abstract, title, pid))


def assert_traceable(paper: Paper, evidence) -> None:
    """Every value must be literally findable in the text it cites, and every offset must be real."""
    source = {"abstract": paper.abstract or "", "title": paper.title}
    squash = lambda t: "".join(t.split()).lower()  # noqa: E731
    spans = []
    for name in ("study_design", "population", "sample_size", "intervention", "comparator", "outcomes", "limitations"):
        field = getattr(evidence, name)
        spans += field.sources
        cited = squash(" ".join(sp.text for sp in field.sources))
        if name in ("population", "intervention", "comparator") and field.value:
            assert squash(field.value) in cited, (name, field.value)
        if name in ("outcomes", "limitations"):
            for value in field.values:
                assert squash(value) in cited, (name, value)
        if name == "sample_size":
            digits = squash(rule_based._numbers_as_digits(" ".join(sp.text for sp in field.sources)))
            for number in (field.participants, field.studies):
                if number is not None:
                    assert str(number) in digits or f"{number:,}" in digits, (name, number)
    for finding in evidence.findings.items:
        spans.append(finding.source)
        for stat in finding.statistics:
            assert squash(stat) in squash(finding.source.text)
    for span in spans:
        assert source[span.origin][span.start : span.end] == span.text


COMPLETE = (
    "BACKGROUND: Chronic low back pain is common.\n"
    "OBJECTIVE: To investigate the effect of Pilates on pain and disability compared with home exercise in adults "
    "with chronic low back pain.\n"
    "METHODS: A randomised controlled trial. 120 adults with chronic low back pain were randomly allocated to Pilates or "
    "home exercise. Outcomes were pain intensity, disability, and quality of life.\n"
    "RESULTS: Pain fell more with Pilates (mean difference -1.1; 95% CI -2.0 to -0.2; p = 0.01).\n"
    "CONCLUSION: Pilates was superior."
)


# --- 1. a complete abstract ---------------------------------------------------------------------------


def test_complete_structured_abstract_gives_every_major_field():
    ev = extract(COMPLETE, pid="pubmed:101")

    assert ev.abstract_status == "present"
    assert (ev.study_design.status, ev.study_design.value) == (FieldStatus.STATED, "randomized controlled trial")
    assert ev.population.value == "adults with chronic low back pain"
    assert ev.sample_size.participants == 120 and ev.sample_size.studies is None
    assert ev.intervention.value == "Pilates"
    assert ev.comparator.value == "home exercise"
    assert ev.outcomes.values == ["pain intensity", "disability", "and quality of life"][:2] + ["quality of life"]
    assert [f.kind for f in ev.findings.items] == ["result", "conclusion"]
    assert ev.findings.items[0].statistics == ["95% CI -2.0 to -0.2", "p = 0.01"]
    assert ev.limitations.status is FieldStatus.UNAVAILABLE  # the abstract states none: it is not invented
    assert_traceable(make_paper(COMPLETE, pid="pubmed:101"), ev)


# --- 2. no sample size ---------------------------------------------------------------------------------


def test_abstract_without_a_sample_size_leaves_it_unavailable():
    ev = extract("OBJECTIVE: To test whether yoga helps adults with anxiety.\nRESULTS: Anxiety fell.")
    assert ev.sample_size.status is FieldStatus.UNAVAILABLE
    assert ev.sample_size.participants is None and ev.sample_size.studies is None


def test_group_sizes_are_never_added_up_into_a_total():
    ev = extract("METHODS: Adults were randomly allocated to Pilates (n = 72) or home exercise (n = 73).")
    assert ev.sample_size.participants is None  # 145 would be a guess


def test_a_per_group_count_is_not_the_sample_size():
    assert extract("METHODS: We included 30 patients per group.").sample_size.participants is None


def test_counts_of_a_subset_or_a_step_are_not_the_sample_size():
    ev = extract("METHODS: Of the 288 people interested in participating, 42 completed the program.")
    assert ev.sample_size.participants is None
    assert extract("METHODS: 1582 individuals were screened for eligibility.").sample_size.participants is None


def test_several_competing_totals_are_ambiguous_so_nothing_is_reported():
    ev = extract("METHODS: 158 individuals were enrolled.\nRESULTS: 90 participants were randomly assigned and 86 were analysed.")
    assert ev.sample_size.participants is None


def test_number_words_are_read_as_stated_but_a_lone_small_number_word_is_not():
    assert extract("METHODS: Seventy-five adults were randomized into 3 groups.").sample_size.participants == 75
    assert extract("METHODS: One hundred and forty-five individuals were enrolled.").sample_size.participants == 145
    assert extract("RESULTS: Thirteen trials involving 345 patients were included.").sample_size.studies == 13
    assert extract("RESULTS: No one dropped out and one of the groups improved.").sample_size.participants is None


def test_number_of_studies_and_participants_in_a_review():
    ev = extract("RESULTS: A total of 29 studies (n=2668) were included.")
    assert (ev.sample_size.studies, ev.sample_size.participants) == (29, 2668)


def test_sample_size_with_thousands_separator():
    assert extract("METHODS: 1,234 patients were recruited.").sample_size.participants == 1234


def test_a_count_of_zero_is_not_a_sample_size():
    assert extract("RESULTS: 0 studies met the inclusion criteria and were included.").sample_size.studies is None


# --- 3. no comparator ----------------------------------------------------------------------------------


def test_single_arm_study_has_no_comparator_but_that_is_not_recorded_as_none():
    ev = extract("OBJECTIVE: To evaluate a 12-week walking program in adults with knee osteoarthritis.")
    assert ev.comparator.status is FieldStatus.UNAVAILABLE
    assert ev.comparator.value is None  # "unavailable" is NOT "there was no comparator"


@pytest.mark.parametrize("text", ["This study had no control group.", "We worked without a placebo group."])
def test_a_negated_comparator_is_not_extracted(text):
    assert extract("METHODS: " + text).comparator.status is FieldStatus.UNAVAILABLE


def test_comparator_from_compared_with_and_versus():
    assert extract("OBJECTIVE: To compare Pilates with home exercise.\nMETHODS: Pilates versus home exercise was tested.").comparator.value == "home exercise"
    assert extract("METHODS: Participants received yoga compared with usual care in adults.").comparator.value == "usual care"


def test_a_comparator_in_a_results_sentence_is_not_used():
    # "compared to the participants in Group 2" describes a result, not the study's comparator
    ev = extract("This trial tested yoga in adults. The yoga group improved more compared to the participants in Group 2.")
    assert ev.comparator.status is FieldStatus.UNAVAILABLE


def test_labelled_comparator_section():
    ev = extract("INTERVENTION: Pilates twice a week.\nCOMPARISON: Usual physiotherapy.")
    assert ev.intervention.value == "Pilates twice a week."
    assert ev.comparator.value == "Usual physiotherapy."


# --- 4. missing abstract (and how it differs from an empty one) -------------------------------------------


def test_missing_abstract_is_reported_as_missing_and_nothing_is_invented():
    ev = extract(None, title="Exercise for back pain: a protocol")

    assert ev.abstract_status == "missing"
    assert ev.sample_size.status is FieldStatus.UNAVAILABLE
    assert ev.intervention.status is FieldStatus.UNAVAILABLE
    assert ev.comparator.status is FieldStatus.UNAVAILABLE
    assert ev.outcomes.status is FieldStatus.UNAVAILABLE
    assert ev.findings.status is FieldStatus.UNAVAILABLE
    assert ev.limitations.status is FieldStatus.UNAVAILABLE


@pytest.mark.parametrize("empty", ["", "   ", "\n \n"])
def test_an_empty_abstract_is_a_different_state_from_a_missing_one(empty):
    ev = extract(empty)
    assert ev.abstract_status == "empty"
    assert ev.unavailable_fields() == list(ev.FIELD_NAMES)


def test_missing_and_empty_and_present_are_three_distinct_states():
    assert {extract(None).abstract_status, extract("").abstract_status, extract("Some text.").abstract_status} == {"missing", "empty", "present"}


def test_only_the_title_can_help_when_there_is_no_abstract_and_it_is_marked_inferred():
    ev = extract(None, title="Pilates for adults with chronic back pain: a randomized controlled trial")

    assert (ev.study_design.status, ev.study_design.value) == (FieldStatus.INFERRED, "randomized controlled trial")
    assert ev.study_design.sources[0].origin == "title"
    assert (ev.population.status, ev.population.value) == (FieldStatus.INFERRED, "adults with chronic back pain")
    assert ev.sample_size.status is FieldStatus.UNAVAILABLE  # the title is never used for counts


def test_a_title_never_supplies_intervention_comparator_outcomes_or_findings():
    ev = extract(None, title="Effect of exercise on pain compared with usual care")
    assert ev.intervention.status is ev.comparator.status is ev.outcomes.status is FieldStatus.UNAVAILABLE
    assert ev.findings.status is ev.limitations.status is FieldStatus.UNAVAILABLE


def test_the_abstract_beats_the_title_and_is_marked_stated():
    ev = extract("DESIGN: A cohort study.", title="A randomized controlled trial")
    assert (ev.study_design.status, ev.study_design.value) == (FieldStatus.STATED, "cohort study")


# --- 5. structured abstracts keep their section labels ----------------------------------------------------------


def test_sources_remember_which_abstract_section_they_came_from():
    ev = extract(COMPLETE)

    assert ev.sample_size.sources[0].section == "METHODS"
    assert ev.intervention.sources[0].section == "OBJECTIVE"
    assert [f.source.section for f in ev.findings.items] == ["RESULTS", "CONCLUSION"]


def test_labelled_sections_are_preferred_over_sentence_patterns():
    ev = extract("OBJECTIVE: To study adults with asthma.\nPARTICIPANTS: Children aged 6-12 years with eczema.")
    assert ev.population.value == "Children aged 6-12 years with eczema."
    assert ev.population.sources[0].section == "PARTICIPANTS"


def test_unlabelled_abstract_has_sources_without_a_section_and_unclassified_findings():
    ev = extract("We tested yoga in 40 adults with anxiety. Anxiety improved significantly (p < 0.01).")
    assert all(s.section is None for s in ev.sample_size.sources)
    assert [f.kind for f in ev.findings.items] == ["unclassified"]


# --- 6. several outcomes ------------------------------------------------------------------------------------


def test_multiple_outcomes_become_separate_items():
    ev = extract("METHODS: The primary outcomes were pain intensity, disability, quality of life and sleep.")
    assert ev.outcomes.values == ["pain intensity", "disability", "quality of life", "sleep"]


def test_outcomes_from_an_effect_of_x_on_y_aim():
    ev = extract("OBJECTIVE: To examine the effect of yoga on pain, function and mood in adults.")
    assert ev.outcomes.values == ["pain", "function", "mood"] or ev.outcomes.values == ["pain", "function and mood"]
    assert "pain" in ev.outcomes.values


def test_a_coordinated_phrase_without_commas_is_kept_whole_not_split():
    # splitting "physiological and cognitive responses" at "and" would leave a meaningless "physiological"
    ev = extract("OBJECTIVE: To assess the effects of sleep loss on physiological and cognitive responses to exercise.")
    assert ev.outcomes.values == ["physiological and cognitive responses to exercise"]


def test_commas_inside_parentheses_do_not_split_an_outcome():
    ev = extract("METHODS: Outcomes were pain (visual analog scale, 0-10), disability, and strength.")
    assert ev.outcomes.values == ["pain (visual analog scale, 0-10)", "disability", "strength"]


def test_labelled_outcomes_section():
    ev = extract("MAIN OUTCOME MEASURES: Pain intensity, function, and anxiety.")
    assert ev.outcomes.values == ["Pain intensity", "function", "anxiety"]


def test_a_description_of_the_people_studied_is_not_an_outcome():
    ev = extract("PURPOSE: To investigate the effect of taping on a part-time worker with shoulder pain.")
    assert ev.outcomes.status is FieldStatus.UNAVAILABLE


def test_student_performance_is_a_valid_outcome():
    ev = extract("BACKGROUND: This study evaluated the impact of teaching methods on student performance in dentistry.")
    assert ev.outcomes.values == ["student performance"]


def test_outcomes_in_a_results_or_conclusion_sentence_are_ignored():
    ev = extract("RESULTS: The effect of yoga on pain was large.\nCONCLUSION: The impact of yoga on anxiety is clear.")
    assert ev.outcomes.status is FieldStatus.UNAVAILABLE


# --- 7. findings that contain numbers ----------------------------------------------------------------------------


def test_statistics_are_copied_verbatim_in_order():
    text = "Pain fell (SMD -0.5; 95% CI -0.9 to -0.1; p < 0.001) and 12.5% improved (OR 1.8, d = 0.8, r = 0.45)."
    assert statistics_in(text) == ["SMD -0.5", "95% CI -0.9 to -0.1", "p < 0.001", "12.5%", "OR 1.8", "d = 0.8", "r = 0.45"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("p<0.05", ["p<0.05"]),
        ("P = .03", ["P = .03"]),
        ("p ≤ 0.02", ["p ≤ 0.02"]),
        ("p=0·0013", ["p=0·0013"]),  # a middle dot as the decimal mark must not be cut to "p=0"
        ("95% CI, -0.38 to 0.23", ["95% CI, -0.38 to 0.23"]),
        ("95% confidence interval [CI] = 0.57 to 1.15", ["95% confidence interval [CI] = 0.57 to 1.15"]),
        ("95% CI -1·9 to -0·5", ["95% CI -1·9 to -0·5"]),
        ("Hedges g = 0.55", ["Hedges g = 0.55"]),
        ("improved by 75%", ["75%"]),
        ("no numbers here", []),
    ],
)
def test_statistic_formats(text, expected):
    assert statistics_in(text) == expected


def test_findings_with_numbers_keep_the_numbers_with_their_sentence():
    ev = extract("RESULTS: Pain fell by 2.1 points (p < 0.001). Disability improved (95% CI 0.2 to 0.8).\nCONCLUSION: It worked.")

    assert [f.source.text for f in ev.findings.items] == [
        "Pain fell by 2.1 points (p < 0.001).",
        "Disability improved (95% CI 0.2 to 0.8).",
        "It worked.",
    ]
    assert [f.statistics for f in ev.findings.items] == [["p < 0.001"], ["95% CI 0.2 to 0.8"], []]


def test_statistics_are_never_computed_or_rewritten():
    ev = extract("RESULTS: Group A scored 12 and group B scored 8 (p = 0.04).")
    assert ev.findings.items[0].statistics == ["p = 0.04"]  # no difference of 4 is made up


def test_in_an_unlabelled_abstract_only_result_like_sentences_are_findings():
    ev = extract("To compare yoga with rest. Pain improved significantly. We enrolled adults.")
    assert [f.source.text for f in ev.findings.items] == ["Pain improved significantly."]


def test_background_methods_sentences_are_not_findings_in_a_labelled_abstract():
    ev = extract("BACKGROUND: Pain improved in many studies.\nMETHODS: We reduced doses.\nRESULTS: Pain fell.")
    assert [f.source.text for f in ev.findings.items] == ["Pain fell."]


# --- 8. missing intervention ---------------------------------------------------------------------------------------


def test_a_descriptive_study_has_no_intervention():
    ev = extract("OBJECTIVE: To describe how common neck pain is among office workers.\nMETHODS: A survey was sent to 400 workers.")
    assert ev.intervention.status is FieldStatus.UNAVAILABLE
    assert ev.intervention.value is None


def test_an_intervention_is_only_taken_from_an_explicit_phrase():
    assert extract("OBJECTIVE: To examine the efficacy of dry needling for neck pain.").intervention.value == "dry needling"
    assert extract("METHODS: Participants were randomized to yoga or usual care.").intervention.value == "yoga"
    assert extract("METHODS: We measured pain at baseline.").intervention.status is FieldStatus.UNAVAILABLE


def test_phrases_are_not_cut_at_in_combination_with():
    ev = extract("AIMS: To investigate the effect of exercise alone or in combination with manual therapy on pain.")
    assert ev.intervention.value == "exercise alone or in combination with manual therapy"


def test_dangling_connectors_and_generic_words_are_removed_or_rejected():
    assert extract("OBJECTIVE: To compare yoga versus participants.").comparator.status is FieldStatus.UNAVAILABLE
    assert extract("OBJECTIVE: To study the effect of exercise or on pain.").intervention.value == "exercise"


# --- 9. evidence is tied to the right paper ----------------------------------------------------------------------------


def test_evidence_carries_the_papers_canonical_id():
    assert extract(COMPLETE, pid="pubmed:12345").paper_id == "pubmed:12345"
    assert extract(None, pid="crossref:10.1000/x").paper_id == "crossref:10.1000/x"


def test_different_papers_give_evidence_with_their_own_ids():
    a, b = extract("METHODS: A cohort study.", pid="pubmed:1"), extract("METHODS: A case series.", pid="pubmed:2")
    assert (a.paper_id, a.study_design.value) == ("pubmed:1", "cohort study")
    assert (b.paper_id, b.study_design.value) == ("pubmed:2", "case series")


def test_the_extractor_records_its_own_name_and_the_interface_is_async():
    paper = make_paper(COMPLETE)
    via_async = asyncio.run(extractor.extract(paper))

    assert via_async == extractor.extract_sync(paper)
    assert via_async.extractor == extractor.name == "rule-based-v1"


def test_extraction_does_not_modify_the_paper():
    paper = make_paper(COMPLETE)
    before = paper.model_dump()
    extractor.extract_sync(paper)
    assert paper.model_dump() == before


def test_extraction_is_deterministic():
    paper = make_paper(COMPLETE)
    assert extractor.extract_sync(paper) == extractor.extract_sync(paper)


# --- 10. nothing is fabricated when information is absent -----------------------------------------------------------------


@pytest.mark.parametrize(
    "abstract",
    [
        "Sleep deprivation causes physiological alterations. These effects indicate a capacity-limited system. We conclude with implications.",
        "Musculoskeletal disorders affect millions worldwide. This review examines recent advancements in diagnosis.",
        "x",
        "1234 5678 !!! ??? ...",
        "The the the the. And and and. Of of of.",
        "METHODS:\nRESULTS:\nCONCLUSION:",
    ],
)
def test_text_that_states_nothing_gives_no_values_at_all(abstract):
    ev = extract(abstract, title="Notes")

    assert ev.available_fields() == [] or ev.available_fields() == ["findings"]
    assert ev.study_design.value is None and ev.population.value is None
    assert ev.sample_size.participants is None and ev.sample_size.studies is None
    assert ev.intervention.value is None and ev.comparator.value is None
    assert ev.outcomes.values == [] and ev.limitations.values == []


def test_a_review_without_methods_gets_no_design_population_or_sample():
    ev = extract(
        "This review examines recent advancements in the diagnosis and management of myofascial pain. "
        "The paper discusses the implications of various treatments, such as dry needling and manual therapy.",
        title="Advancing musculoskeletal diagnosis and therapy: a comprehensive review",
    )
    assert ev.study_design.status is FieldStatus.UNAVAILABLE
    assert ev.sample_size.status is FieldStatus.UNAVAILABLE
    assert ev.population.status is FieldStatus.UNAVAILABLE


def test_every_extracted_value_is_traceable_in_all_real_snapshot_papers():
    pools = json.loads((EVAL_DIR / "pools.json").read_text(encoding="utf-8"))
    checked = 0
    for entry in pools["questions"].values():
        for provider in entry["providers"].values():
            for raw in provider:
                paper = Paper(**raw)
                evidence = extractor.extract_sync(paper)
                assert evidence.paper_id == paper.id
                assert_traceable(paper, evidence)
                checked += 1
    assert checked >= 380


# --- study designs: one test per label, so a broken pattern cannot go unnoticed ----------------------------------------


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("A randomised controlled trial with a six-month follow-up.", "randomized controlled trial"),
        ("This randomized clinical trial included adults.", "randomized controlled trial"),
        ("Participants were randomly assigned to two groups.", "randomized controlled trial"),
        ("We conducted a quasi-experimental study.", "quasi-experimental study"),
        ("A non-randomised controlled trial was run.", "quasi-experimental study"),
        ("A systematic review was performed.", "systematic review"),
        ("A meta-analysis of cohort data was performed.", "meta-analysis"),
        ("This systematic review and meta-analysis aimed to test X.", "systematic review with meta-analysis"),
        ("An umbrella review of systematic reviews was done.", "umbrella review"),
        ("A scoping review was conducted.", "scoping review"),
        ("This narrative review summarises the field.", "narrative review"),
        ("This prospective cohort study followed adults.", "cohort study"),
        ("We used a case-control design.", "case-control study"),
        ("A cross-sectional survey was done.", "cross-sectional study"),
        ("We report a case series of 5 patients.", "case series"),
        ("The purpose of this case report was to describe one patient.", "case report"),
        ("We carried out a qualitative study with interviews.", "qualitative study"),
        ("This study protocol describes a planned trial.", "study protocol"),
    ],
)
def test_each_study_design_is_recognised(text, label):
    assert extract("METHODS: " + text).study_design.value == label


def test_a_review_that_includes_randomized_trials_is_a_review_not_a_trial():
    ev = extract("METHODS: We searched PubMed and Embase. Randomized controlled trials were included.\nRESULTS: 12 trials were pooled in a meta-analysis.")
    assert ev.study_design.value == "systematic review with meta-analysis"


def test_a_cochrane_style_review_is_recognised_from_its_search_sections():
    ev = extract("SEARCH METHODS: We searched CENTRAL and MEDLINE.\nSELECTION CRITERIA: Randomised controlled trials of exercise.")
    assert ev.study_design.value == "systematic review"


def test_cross_sectional_area_is_a_measurement_not_a_design():
    ev = extract("METHODS: Muscle fibre cross-sectional area was measured from biopsies.")
    assert ev.study_design.status is FieldStatus.UNAVAILABLE


def test_designs_mentioned_as_background_about_other_studies_are_ignored():
    ev = extract("Previous randomized trials found benefits. Several systematic reviews agree. We describe one patient.")
    assert ev.study_design.status is FieldStatus.UNAVAILABLE


def test_a_design_named_only_in_the_background_section_is_ignored():
    ev = extract("BACKGROUND: Randomized controlled trials are the gold standard.\nMETHODS: We interviewed ten nurses.")
    assert ev.study_design.status is FieldStatus.UNAVAILABLE


def test_a_weak_search_cue_does_not_override_an_explicit_narrative_review_title():
    ev = extract("PubMed and EBSCOHost databases were searched through 2016 for all HIIT studies.", title="HIIT in diabetes: A Narrative Review")
    assert (ev.study_design.status, ev.study_design.value) == (FieldStatus.INFERRED, "narrative review")


def test_a_systematic_review_stated_in_the_abstract_wins_over_the_title():
    ev = extract("METHODS: This systematic review searched PubMed.", title="HIIT in diabetes: A Narrative Review")
    assert (ev.study_design.status, ev.study_design.value) == (FieldStatus.STATED, "systematic review")


# --- limitations -----------------------------------------------------------------------------------------------------------


def test_an_explicit_study_limitation_is_extracted():
    ev = extract("CONCLUSION: A main limitation was the small sample size. Results should be interpreted with caution.")
    assert ev.limitations.status is FieldStatus.STATED
    assert len(ev.limitations.values) == 2


@pytest.mark.parametrize(
    "text",
    [
        "Cognitive performance relies on a capacity-limited system with capacity limitations.",  # a limitation of the brain
        "Medical students reported various limitations and challenges of online education.",  # of the thing studied
        "There are limitations of medication use during pregnancy.",
        "Further studies are warranted.",
        "We found no limitations in the data.",
    ],
)
def test_text_that_merely_contains_the_word_limitation_is_not_a_study_limitation(text):
    assert extract("RESULTS: " + text).limitations.status is FieldStatus.UNAVAILABLE


def test_a_limitations_section_counts():
    ev = extract("LIMITATIONS: Only one centre took part.")
    assert ev.limitations.values == ["Only one centre took part."]


# --- guard rails against the extractor itself being broken ------------------------------------------------------------------


def test_the_source_file_has_no_stray_control_characters():
    """An earlier editing slip put backspace characters where regex word boundaries belong and silently disabled patterns."""
    source = Path(rule_based.__file__).read_text(encoding="utf-8")
    assert {hex(c) for c in map(ord, source) if c < 32 and c not in (9, 10)} == set()


def test_all_patterns_that_use_word_boundaries_really_contain_them():
    for label, pattern in rule_based._DESIGNS_BEFORE_REVIEWS + rule_based._DESIGNS_AFTER_REVIEWS:
        assert "\x08" not in pattern.pattern, label
        assert "\\b" in pattern.pattern, label


@pytest.mark.parametrize("junk", ["", " ", "\x00\x01", "((((", "<" * 5000, "a. " * 3000, "AB: " * 500, "p < 0.05 " * 2000, "ä€𝔘" * 500])
def test_odd_input_never_crashes_and_stays_valid(junk):
    ev = extract(junk)
    assert ev.paper_id == "pubmed:1"


def test_extraction_time_grows_linearly_with_abstract_length():
    base = COMPLETE + "\n"
    small, large = make_paper(base * 5), make_paper(base * 40)  # 8x the text

    def best(paper):
        times = []
        for _ in range(3):
            start = time.perf_counter()
            extractor.extract_sync(paper)
            times.append(time.perf_counter() - start)
        return min(times)

    assert best(large) < max(best(small), 0.002) * 40  # linear ~8x; quadratic would be ~64x


def test_a_hostile_long_unclosed_input_is_fast():
    start = time.perf_counter()
    extractor.extract_sync(make_paper("of " * 20000 + "effect of " * 3000 + "on " * 3000))
    assert time.perf_counter() - start < 3


def test_a_question_mark_from_a_title_is_not_kept_in_an_inferred_value():
    ev = extract(None, title="Is CBT effective in individuals with chronic pain?")
    assert ev.population.value == "individuals with chronic pain"
    assert ev.population.status is FieldStatus.INFERRED
