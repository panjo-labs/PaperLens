# CLAUDE_CONTEXT.md

Persistent context for future Claude Code sessions. It was written by inspecting the repository (code, tests, docs, evaluation
files, git), not from conversation memory. If this file and the code disagree, **the code wins**: verify, then fix this file.
Snapshot date: 2026-10-04. Detailed designs live in `ARCHITECTURE.md`; open issues in `TODO.md`; requirements in `PROJECT_SPEC.md`.

## 1. Project identity

- **Name:** PaperLens (GitHub: `panjo-labs/PaperLens`). Inside the repo the product is called **"Evidence-first Research Assistant"**
  (spec, architecture); the Python package is `research-assistant-backend`. The string "PaperLens" appears nowhere in the code or docs.
- **What it does:** takes a natural-language research question, searches academic literature (PubMed, Crossref), returns de-duplicated,
  ranked papers, and extracts structured evidence from each. Planned (not built): comparison, contradiction and gap detection,
  citation-grounded synthesis written by an open-weight LLM.
- **Primary users (spec section 1):** university students, researchers, thesis writers, academic project teams. Initial domain: general
  academic research with strong early support for health/physiotherapy literature; the architecture must stay domain-agnostic.
- **Core principle (spec section 2): evidence first.** Every substantive claim must trace to retrieved evidence. Prefer "insufficient
  evidence" over a confident unsupported answer. Never fabricate papers, citations, DOIs, findings, statistics, authors or gaps.
- **Current development objective:** a trustworthy structured-evidence layer (done and committed) so that the next slice can add a
  grounded LLM on top of it.

## 2. Current architecture (as implemented)

```text
Frontend                       NOT IMPLEMENTED
    |
FastAPI  (app/main.py, app/api/research.py)       POST /research, GET /health
    |
Research Orchestrator  (app/services/orchestrator.py)
    |-- 1 Query Processor     app/services/query_processor.py   deterministic, no LLM
    |-- 2 Academic Search     PubMed + Crossref searched CONCURRENTLY (asyncio.gather), each with its own time budget
    |       |-- PubMed        app/providers/pubmed.py + pubmed_parser.py      (esearch -> efetch, XML)
    |       |-- Crossref      app/providers/crossref.py + crossref_parser.py  (/works JSON)
    |       (shared: providers/http.py retry/timeout, providers/rate_limit.py, providers/base.py interface)
    |-- 3 Deduplicator        app/services/deduplicator.py
    |-- 4 Paper Ranker        app/services/ranker.py            lexical baseline
    `-- 5 Evidence Extraction app/services/evidence/            rule-based, NO LLM
            |
            v
    [future: open-weight LLM behind a provider abstraction]     NOT IMPLEMENTED (no LLM code or dependency exists)
```

Order inside `ResearchOrchestrator.research` (see its numbered comments): process question -> search providers concurrently ->
combine in provider order (PubMed first) -> deduplicate -> rank -> extract evidence -> `ResearchResponse`.
`provider_ranks` is stamped on each paper right after its provider returns. Evidence is a **separate** dict keyed by paper id.

Layout (`backend/`): `app/{api,schemas,providers,services}`, `tests/` (+ `tests/fixtures/`), `evaluation/`, `pyproject.toml`,
`.env.example` (keys only: `NCBI_EMAIL`, `NCBI_API_KEY`, `CROSSREF_MAILTO`). No frontend directory, no database, no deployment files.

## 3. Completed slices

Commits are on `main` (see section 11). Hashes verified with `git log`.

### Slice 1: question -> PubMed -> `Paper[]` (commit `d612a76`, pushed)
- **Purpose:** the first vertical slice and the spec's first milestone.
- **Built:** `Paper` model; query processor (drops question/prompt words, keeps acronyms like WHO/US, strips operators); PubMed esearch (`sort=relevance`,
  retmax 20) then efetch; `defusedxml` parser (structured abstracts keep labels, missing abstract is `None`, dates keep PubMed's precision,
  collective authors, DOI taken only from the article's own IDs, not its reference list); `POST /research`.
- **Decisions:** `Paper.id = "pubmed:<PMID>"`; one shared `httpx.AsyncClient`; 10 s per-request timeout; one retry on 429/5xx honoring `Retry-After`
  (capped 5 s); NCBI `tool`/`email`/optional API key; request-rate limiter (3/s, 10/s with key); 18 s per-provider time budget; settings validated.
- **Verified by:** unit tests with recorded real PubMed XML fixtures plus mocked-HTTP provider and end-to-end API tests.

### Slice 2A: Crossref as a second provider (commit `e82ad67`, pushed)
- **Built:** `CrossrefProvider` and defensive parser (JATS abstracts to plain text, partial dates, org authors, missing DOI/title -> record skipped,
  filter `type:journal-article`); shared `ProviderHttp` extracted from PubMed; providers run concurrently; failures degrade per provider.
- **Response contract:** `provider_status` per provider; HTTP 200 on partial failure, 502 only if every provider fails.
- **Also in this commit:** plain-language comments across `app/`, and a fix so abstract cleaning is linear-time on malformed markup.
- **Verified by:** recorded real Crossref fixture, mocked failure/timeout/429 tests, mutation checks that sequential execution fails the concurrency tests.

### Slice 2B: deduplication and ranking (commit `81c423f`, pushed)
- **Deduplicator:** union-find over keys, O(n): normalized DOI, then PMID (from `source_ids`), then exact normalized title **only for records with neither**.
  Merge keeps the first record's id and the most complete metadata; `Paper.source_ids` records provenance.
- **Ranker:** `0.75 x title coverage + 0.25 x abstract coverage` over non-stopword terms (plural folding, abstract credit capped at 3 mentions); deterministic
  tie-breaks (score, has abstract, newer date, id); result in `Paper.rank_score`. Documented as a retrieval heuristic, not scientific relevance.
- **Evaluation:** `backend/evaluation/` (10 questions, frozen provider snapshot `pools.json`, 80 judged-relevant papers). Result: ranker is **about equal to
  PubMed's own order** (means baseline vs ranked: recall@5 0.26/0.28, recall@10 0.43/0.48, recall@20 0.74/0.64, MRR 0.71/0.63; `evaluation/RESULTS.md`).

### Provider rank preservation (commit `af40a9c`, pushed)
- `Paper.provider_ranks` e.g. `{"pubmed": 3, "crossref": 12}`: each provider's own position, kept through combine -> dedup (best rank per provider on merge)
  -> ranking. **Stored but not used for ordering**; intended for future rank fusion. Tests: `tests/test_provider_rank_preservation.py`.

### Slice 3: evidence extraction (commit `75cb424`, pushed; see section 5)
- Also committed separately: `7cad0af` fixes Crossref abstracts whose paragraph tags were entity-encoded (`&lt;p&gt;`).

## 4. Current state

- **Works (verified by tests and live runs):** the full pipeline above through `POST /research`, including evidence. A live run on four questions returned
  evidence for every paper, with ids/order consistent with `papers`, in about 3.3-4.1 s per request.
- **Tests:** **527 tests collected** (`pytest --collect-only`); the last full runs all passed (527 passed in about 31 s, repeated 5 times, 0 flaky).
  By file: crossref_parser 78, evidence_extractor 113, deduplicator 49, pubmed_provider 41, pubmed_parser 30, ranker 30, crossref_provider 25,
  evidence_schema 24, orchestrator 18, evidence_pipeline 18, query_processor 18, provider_rank_preservation 17, research_integration 16,
  evidence_evaluation 14, abstract_sections 12, research_api 11, evaluation_dataset 5, pipeline_performance 5, rate_limit 3.
  All network is mocked (`respx`); no test needs the internet. All runs were on **Python 3.10.11** (see section 9).
- **Evaluation status:** retrieval evaluation and evidence evaluation both exist and run offline (`python evaluation/run_eval.py`,
  `python evaluation/run_evidence_eval.py [--holdout] [--write]`). Results in section 5 and `evaluation/*RESULTS.md`.
- **API endpoints (the only two defined in code):**
  - `POST /research` body `{"question": str (3-1000 chars)}` -> `question`, `queries_used`, `papers`, `provider_status`, `evidence`.
    Status: 200 (including zero results or one provider failing), 422 (bad body, or a question with no searchable terms), 502 (every provider failed; same body shape).
  - `GET /health` -> `{"status": "ok"}`.
- **Production dependencies** (`pyproject.toml`): fastapi, uvicorn, httpx, pydantic (v2), pydantic-settings, defusedxml. `requires-python = ">=3.11"`.
- **Dev/test dependencies:** pytest, pytest-asyncio (`asyncio_mode = "auto"`), respx.
- **Configuration (env vars, via `app/config.py`):** `NCBI_EMAIL` (required, fails fast), `NCBI_API_KEY`, `CROSSREF_MAILTO` (optional), plus tunables with
  validated bounds (`PUBMED_RETMAX` 1-100 default 20, `CROSSREF_ROWS` 1-100 default 20, `HTTP_TIMEOUT_SECONDS` default 10, `PROVIDER_TIME_BUDGET_SECONDS` default 18).
  Never commit `.env`; it is gitignored and none exists in the repo.
- **Known limitations:** section 9.

## 5. Evidence extraction (Slice 3)

**Deterministic today; no LLM, no network, no model dependency anywhere.** An LLM is intended later only to fill what rules cannot (section 6).

- **Schema** (`app/schemas/evidence.py`). `Evidence`: `paper_id` (required, non-blank), `extractor` (e.g. `rule-based-v1`),
  `abstract_status` in {`present`, `empty`, `missing`}, and fields `study_design`, `population`, `sample_size` (`participants`, `studies`), `intervention`,
  `comparator`, `outcomes` (list), `findings` (sentences with `kind` result/conclusion/unclassified plus verbatim `statistics`), `limitations` (list).
  Every field has `status`: **`stated`** (in the abstract), **`inferred`** (from the title only; used for study design and population),
  **`unavailable`**. Validators enforce: unavailable fields are empty; stated/inferred values must cite a `SourceSpan`; stated cites only the abstract, inferred only the title;
  counts >= 1; `study_design` from a fixed vocabulary (15 labels, e.g. randomized controlled trial, systematic review with meta-analysis, cohort study, study protocol).
- **Meaning of unavailable:** "not found", never "absent". A missing comparator is not "no comparator". Missing data is never negative evidence.
- **Interface** (`app/services/evidence/base.py`): `EvidenceExtractor` Protocol with `name` and `async extract(paper) -> Evidence` (async so a model-backed extractor can drop in).
  The orchestrator discards evidence whose `paper_id` does not match the paper and skips (logs) a paper whose extraction raises; absent from `evidence` means failed, never guessed.
- **Method** (`app/services/evidence/rule_based.py`, `RuleBasedEvidenceExtractor`): conservative phrase patterns; first match wins; ambiguity becomes unavailable.
  Examples: designs checked in priority order (reviews before trials, so a review that includes RCTs is not called an RCT; "cross-sectional area" is not a design;
  background-only mentions ignored); participants prefer "N patients" over per-group `n =`, ignore subsets ("42 completed", "288 interested"), and are never summed;
  number words ("Seventy-five", "Thirteen") are read but a lone "one".."nine" is not; intervention/comparator/outcomes come from labelled sections or explicit phrases
  ("effect of X on Y", "compared with Z", "outcomes were ..."), only from aim/method sentences, never from results; outcome lists split only on commas; statistics copied verbatim
  (p-values, 95% CI, effect-size letters, percentages; middle-dot decimals accepted); limitations only when the text names a limitation of the study.
- **Sentence splitting** (`app/services/evidence/sections.py`): splits an abstract into labelled sections (`BACKGROUND:`, `DESIGN, SETTING, AND PARTICIPANTS:` etc.; a label must look like a
  heading) and sentences, handling abbreviations ("vs.", "e.g.", initials) and decimals; every `Sentence` has offsets such that `abstract[start:end] == text`.
- **Source traceability:** each value cites the verbatim sentence, its section label, and real offsets into `Paper.abstract` / `Paper.title`; offsets are never invented.
  Audit result: 0 values absent from their cited text across all 389 snapshot papers (1,731 values) and across 152 live papers (545 values).
- **Missing data:** `abstract_status` separates `missing` (no abstract), `empty` (blank) and `present`. No abstract means only title-inferred design/population are possible.
- **API decision:** evidence is a **separate collection keyed by paper id** (not embedded in `Paper`); rationale in `ARCHITECTURE.md` section 13.4.
- **Evaluation data** (`evaluation/`): `evidence_gold.json` (development, 10 papers, seed 2026) and `evidence_holdout.json` (8 papers, seed 7001, disjoint); expected values were written from
  the text before running the extractor; `run_evidence_eval.py` scores per field (correct / correct-null / missed / wrong / fabricated). `tests/test_evidence_evaluation.py` guards the data.
- **Results (`evaluation/EVIDENCE_RESULTS.md`):**
  - Development set, first run: 51/80 fields (64%). Holdout, first run before any fix to it: **47/64 (73%), the only unbiased estimate**.
  - Current extractor: development 61/80 (76%), holdout 52/64 (81%); both are **tuned on those papers and optimistic**. Statistics recall 100% on both.
  - Weakest fields: outcomes, comparator, intervention. Field unavailability on 263 snapshot papers with abstracts: limitations 90%, comparator 71%, outcomes 63%, sample size 60%.
- **Known accuracy limits:** indirect or abbreviation-defined wording, single-digit number words, ambiguous totals (screened/randomized/analysed), a design only named in the title.
  Expected values are one reviewer's reading (an AI assistant); they have not been independently reviewed. A fresh unseen set is needed before quoting a new accuracy.

## 6. Open-weight model plan

Source of truth for this section: `PROJECT_SPEC.md` (sections 4.7, 6, 7), `ARCHITECTURE.md` (sections 15-17), `TODO.md` ("Slice 4").

- **Status: NOT implemented.** The repository contains no LLM client, no model name, no provider, no prompt code and no LLM dependency (a grep for common model/provider names
  finds nothing). **Do not invent a model or provider.** Choosing one is an open decision. Note the spec lists "self-hosting a large language model" as a **non-goal**.
- **Requirement:** the project must use an **open-weight** model, isolated behind an application interface (`LLMProvider` with `async generate(...)` is the conceptual shape in
  ARCHITECTURE section 15) so the model can be replaced without changing the pipeline. The provider layer owns prompt construction, requests, streaming, error handling and structured-output validation.
- **Expected to do (ARCHITECTURE 16):** evidence extraction (filling what rules cannot), evidence comparison, research synthesis, structured analysis.
- **Must NOT do:** be the source of truth (academic sources are); invent papers, citations, DOIs, findings, statistics, authors or research gaps (spec 4.7); cite anything not retrieved in the
  current workflow; present unsupported inference as fact; replace deterministic steps that already work.
- **Grounding design (ARCHITECTURE 17):** question -> retrieved papers -> structured evidence -> LLM -> draft synthesis -> **citation validation** -> final answer. Validation must confirm every
  citation exists, maps to a retrieved paper, and reject unknown ids. Any model-backed `EvidenceExtractor` must obey the contract in `app/schemas/evidence.py` (cite source text, mark
  status honestly, leave unsupported fields unavailable). The existing `provider_ranks`, `source_ids` and `Evidence` fields were added to make this possible.

## 7. Architectural decisions (each supported by spec, docs or code)

- Async HTTP with one shared `httpx.AsyncClient`; per-request timeout plus a per-provider total time budget (spec section 8).
- PubMed uses esearch then efetch (efetch gives abstracts); XML parsed with `defusedxml`; results re-ordered to esearch's relevance order.
- Crossref is the second provider (spec 4.2); both sit behind `AcademicSearchProvider` (`providers/base.py`); the rest of the app sees only `Paper`.
- Graceful provider failure (spec 8, ARCHITECTURE 19): one provider failing never discards the other's papers; error messages are hand-written so API keys cannot leak.
- Deterministic ids: `pubmed:<PMID>`, `crossref:<DOI>`; merged papers keep the first record's id (PubMed leads) and list every record in `source_ids`.
- Dedup order DOI -> PMID -> exact title only for records with no DOI/PMID; a title never overrides conflicting DOIs (Cochrane versions share titles). No fuzzy matching.
- `provider_ranks` preserved for future rank fusion; not used yet.
- Lightweight lexical ranking, no ML. **No embeddings or vector database** (spec 4.5 forbids them unless evaluation shows a need).
- **No SQLite yet** (spec allows it; nothing needs persistence so far). **No SSE yet** (spec suggests it for generated responses; nothing is generated yet). No frontend yet.
- **No autonomous agents, no fine-tuning, no unnecessary infrastructure** (spec section 6 non-goals; ARCHITECTURE rules 10-12).
- Evidence-first: nothing is generated that is not traceable; **never fabricate missing evidence** (spec 2 and 4.7, ARCHITECTURE rule 8); unavailable beats a guess.
- Evidence is deterministic first, behind an interface, so an LLM is added only where rules measurably fall short (spec development principle, section 14).

## 8. Important constraints for future sessions

1. Check the repository state (`git status`, source, tests) before acting; never rely on remembered conversation.
2. Do not start a new slice unless the user explicitly says so. Do not silently expand scope or implement future-slice features.
3. Do not add dependencies, databases, services or abstractions without answering the spec's four questions (section 14 of the spec).
4. Do not replace working architecture without a concrete, stated reason.
5. Never fabricate scientific evidence, citations, DOIs, statistics or findings, in code, tests, fixtures or evaluation data. Evaluation "relevant" papers must come from papers really retrieved.
6. Do not claim retrieval or extraction accuracy without measuring it; report which numbers are tuned (optimistic) and which are unbiased.
7. Preserve backwards compatibility of `POST /research` where practical (new fields additive).
8. Run the full test suite after changes (`pytest` in `backend/`) and say if anything fails.
9. Never delete or weaken tests to make the suite pass. Fix the code, or explain a conflict to the user and narrow the change.
10. Keep all network I/O asynchronous with explicit timeouts; keep tests offline (mock with `respx`).
11. Do not add LLM calls where deterministic processing is sufficient; the LLM stays behind an interface.
12. Never commit secrets. Do not read or print `.env`; no API keys or emails in code, docs or fixtures.
13. Respect the "unavailable is not absent" rule when touching evidence code; every stated value must cite real text with real offsets.
14. Practical hazard: writing regexes with backslashes through shell heredocs has corrupted code before (it silently disabled patterns). Use the Edit/Write tools for regex code;
    `tests/test_evidence_extractor.py` has guards that fail on stray control characters in `rule_based.py`.
15. Do not commit or push unless asked.

## 9. Known issues / TODO (summary; details in `TODO.md`)

- **Python version:** only Python 3.10.11 has been available; every test run used it, while `pyproject.toml` requires `>=3.11`. 3.11+ is **untested**. Timing tests have only run on Windows.
- **No deployment config** (`render.yaml`, `.python-version`); Render is the spec's target.
- **Retrieval:** the lexical ranker is only about as good as keeping PubMed's order (section 3); the judged set is one reviewer's reading and skewed toward PubMed (59 of 80 relevant papers); two records of one
  paper with different DOIs are not merged.
- **Missing abstracts:** about a third of papers have none (126 of 389 unique snapshot papers; 58 of 152 in a live run); Crossref rarely supplies abstracts; their evidence is mostly unavailable.
- **Evidence:** honest accuracy about 73% of fields on unseen papers; weak on outcomes/comparator/intervention; fresh unseen evaluation set needed.
- **Payload/latency:** evidence is about 55-60% of the response (80-120 KB for ~40 papers); extraction adds roughly 100-150 ms per request (about 3-5 ms per paper) and runs on the event loop.
- **Repo hygiene:** no `.gitattributes` (LF/CRLF warnings); `evaluation/pools.json` is about 800 KB; Starlette `TestClient` deprecation warning.
- **Query processing:** quotes/brackets/field tags are stripped; lowercase `or`/`not` stay as literal words.

## 10. Current milestone

- **CURRENT:** Slice 3 (structured evidence extraction) is **complete, committed (`75cb424`) and pushed**. It has not had an independent review.
- **NEXT (per `TODO.md`, only if the user instructs):** Slice 4, an LLM provider abstraction with an open-weight model, a model-backed extractor for what rules cannot do,
  citation-grounded synthesis and citation validation. Later: comparison / contradiction / gap analysis, SSE streaming, frontend, Render deployment, whole-pipeline evaluation.
- **Housekeeping pending:** none known. Check `git status` and `git log origin/main..HEAD` for anything newer than this file.

## 11. Git state (as of this snapshot)

- Branch `main`, remote `origin` = `https://github.com/panjo-labs/PaperLens.git`. Everything up to commit `8cb648a` is **pushed**; `origin/main` contained it when this
  file was last updated. Later commits (at least the one that records this) may exist: verify with `git log origin/main..HEAD`. The working tree was clean after committing.
- Commits, oldest first: `263d9a6` Initial commit (LICENSE, .gitignore from GitHub) -> `d612a76` Slice 1 -> `e82ad67` Slice 2A -> `81c423f` Slice 2B ->
  `af40a9c` provider ranks -> `7cad0af` Crossref entity-encoded tag fix -> `75cb424` Slice 3 evidence extraction -> a final docs commit: `ARCHITECTURE.md` and `PROJECT_SPEC.md`
  (renamed with `git mv` from the misnamed `architecture,md.txt` and `project_spec.md.txt`; history preserved), `TODO.md`, and this file.
- No existing test was modified or removed in the Slice 3 work except additions (verified: 0 removed lines in existing tests).

## 12. How a future Claude session should use this file

1. Read `CLAUDE_CONTEXT.md` (this file).
2. Read `PROJECT_SPEC.md`.
3. Read `ARCHITECTURE.md` (sections 13-17 matter most for upcoming work) and `TODO.md`.
4. Inspect the relevant source files before changing code (`app/services/orchestrator.py` is the pipeline's entry point).
5. Run `git status` and `git log --oneline -8`; compare with section 11.
6. Confirm the current slice/milestone with the user (section 10).
7. Do not assume any previous conversation exists.
8. Do not start the next slice unless explicitly instructed.

Commands (from `backend/`; a local `.venv` exists but is gitignored): `python -m pytest -q`; `uvicorn app.main:app --reload` (needs `NCBI_EMAIL` in the environment or `.env`);
`python evaluation/run_eval.py`; `python evaluation/run_evidence_eval.py --write`.
