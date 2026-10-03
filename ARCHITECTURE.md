# Evidence-first Research Assistant Architecture

## Implementation status

This document is the design. See `TODO.md` for what is still to do, and `backend/evaluation/` for measured results.

| Slice | What | Status |
|---|---|---|
| 1 | Research question -> query normalization -> PubMed -> normalized `Paper[]` | done |
| 2A | Crossref provider; providers searched concurrently; graceful per-provider failure | done |
| 2B | Deduplication and lexical ranking; provider rank preserved (`provider_ranks`) | done (ranking is a documented baseline) |
| 3 | Structured evidence extraction (rule-based, no LLM) | done (see section 13) |
| 4 | LLM provider, cited synthesis, citation validation | not started |

The implemented pipeline today:

```text
question -> query processor -> PubMed + Crossref (concurrent) -> deduplicate -> rank -> evidence extraction -> JSON
```

## 1. Architecture Overview

The application follows a layered research pipeline:

```text
┌──────────────────────────────┐
│           Frontend           │
│      React / Next.js         │
└──────────────┬───────────────┘
               │
               │ HTTP / SSE
               ↓
┌──────────────────────────────┐
│           FastAPI            │
│        API Layer             │
└──────────────┬───────────────┘
               │
               ↓
┌──────────────────────────────┐
│    Research Orchestrator     │
│                              │
│  Coordinates the workflow    │
└──────────────┬───────────────┘
               │
       ┌───────┼────────┬──────────────┐
       ↓       ↓        ↓              ↓
   Query    Academic   Paper        Evidence
 Processor   Search    Ranker       Extractor
              │
         ┌────┴─────┐
         ↓          ↓
      PubMed     Crossref
               │
               ↓
        Research Analysis
        ├── Comparison
        ├── Gap Detection
        └── Contradiction Detection
               │
               ↓
        LLM Provider
               │
               ↓
        Open-weight Model
               │
               ↓
     Grounded Research Response
               │
               ↓
          Citations
```

---

## 2. Architectural Principle

The architecture is **evidence-first**.

The LLM should not independently determine the factual basis of an answer.

Instead:

```text
Search
  ↓
Retrieve evidence
  ↓
Structure evidence
  ↓
Analyze evidence
  ↓
Generate synthesis
  ↓
Validate citations
```

The model is primarily responsible for interpreting and synthesizing retrieved evidence.

---

## 3. Frontend

### Responsibility

The frontend provides the user-facing research interface.

Primary responsibilities:

* Research question input
* Search initiation
* Display retrieved papers
* Display evidence
* Display citations
* Display study comparisons
* Display research gaps
* Display conflicting findings
* Stream generated responses
* Allow users to inspect source papers

### Suggested Technology

```text
React / Next.js
Tailwind CSS
```

The frontend should remain relatively thin.

Research logic should live in the backend rather than being duplicated in frontend code.

---

## 4. FastAPI Layer

### Responsibility

FastAPI provides the application API.

Possible endpoints:

```text
POST /research
GET  /papers/{id}
POST /compare
POST /synthesis
GET  /health
```

The exact API surface may evolve during implementation.

The API layer should handle:

* Request validation
* Response schemas
* Authentication if added later
* Streaming responses
* HTTP error handling

It should not contain complex research logic.

---

## 5. Research Orchestrator

The Research Orchestrator is the central application service.

Its responsibility is to coordinate:

```text
Query Processing
       ↓
Academic Search
       ↓
Deduplication
       ↓
Ranking
       ↓
Evidence Extraction
       ↓
Research Analysis
       ↓
LLM Synthesis
       ↓
Citation Validation
```

It should not become an autonomous agent system.

The orchestrator should execute a predictable workflow.

---

## 6. Query Processor

### Input

Natural-language research question.

Example:

```text
Does exercise therapy improve pain and function
in people with chronic low back pain?
```

### Responsibilities

* Clean the input
* Identify the core research intent
* Generate useful search queries
* Preserve important terminology
* Avoid unnecessary query expansion

Possible future enhancements:

* PICO-style query extraction
* Query reformulation
* Domain-specific terminology expansion

These should not be required for the first working version.

---

## 7. Academic Search Layer

The academic search layer abstracts external literature providers.

```text
             Academic Search
                    │
          ┌─────────┴─────────┐
          ↓                   ↓
       PubMed              Crossref
          │                   │
          └─────────┬─────────┘
                    ↓
              Normalized Papers
```

### Design requirement

The rest of the application should not depend directly on PubMed or Crossref response formats.

Each provider should implement a common interface.

Conceptually:

```python
class AcademicSearchProvider:
    async def search(query: str) -> list[Paper]:
        ...
```

Provider-specific implementation details remain inside their respective modules.

---

## 8. PubMed

PubMed is the primary initial source for health and biomedical literature.

Responsibilities:

* Construct PubMed queries
* Execute API requests
* Parse responses
* Normalize metadata
* Handle failures and timeouts

Potential metadata:

```text
PMID
Title
Authors
Abstract
Journal
Publication date
DOI
URL
```

---

## 9. Crossref

Crossref provides broader academic coverage.

Responsibilities:

* Search Crossref
* Parse metadata
* Normalize results
* Handle missing fields
* Handle API failures

The Crossref implementation should produce the same internal `Paper` representation used by PubMed.

---

## 10. Common Paper Model

A provider-independent paper model should be used throughout the system.

Conceptual model:

```python
Paper:
    id
    title
    authors
    abstract
    journal
    publication_date
    doi
    source
    source_id
    url
```

Additional fields may be added when necessary.

The internal ID should be stable within a research session.

**As implemented**, `Paper` also carries three pipeline fields, all backwards compatible (they default to empty):

```text
source_ids       every provider record merged into this paper, e.g. ["pubmed:123", "crossref:10.1000/x"]
provider_ranks   each provider's own position for the paper, e.g. {"pubmed": 3, "crossref": 12}
rank_score       lexical relevance score 0-1 set by the ranker (None until ranked)
```

`provider_ranks` is stored but not yet used for ordering: it exists so a future ranker can fuse our score with each
provider's own relevance order (rank fusion). `rank_score` is a retrieval heuristic, not a measure of scientific quality.

---

## 11. Deduplication

Multiple providers may return the same paper.

Deduplication should occur before evidence extraction.

Preferred order:

```text
DOI
 ↓
Provider-specific identifier
 ↓
Normalized title
 ↓
Additional metadata
```

The system should preserve useful metadata from duplicate records rather than arbitrarily discarding it.

**As implemented** (`app/services/deduplicator.py`): records are grouped by normalized DOI, then PubMed ID, then exact
normalized title **only for records that have neither**. A title never overrides a trustworthy identifier, because
different papers can share a title (successive versions of a Cochrane review have the same title and different DOIs).
The merged paper keeps the first record's id and the most complete metadata, and lists every contributor in `source_ids`.
Known limit: two records of one paper that carry *different* DOIs are not merged.

---

## 12. Paper Ranking

Ranking should prioritize papers most relevant to the user's research question.

Initial ranking can use:

* Query/title relevance
* Abstract relevance
* Metadata
* Publication information
* Availability of an abstract

The first implementation should avoid complex machine-learning ranking systems unless evaluation demonstrates that they are required.

**As implemented** (`app/services/ranker.py`): `0.75 x title coverage + 0.25 x abstract coverage` over the question's
non-stopword terms. On the 10-question evaluation it performed about the same as simply keeping PubMed's own order
(see `backend/evaluation/RESULTS.md`), so it is a baseline, not a finished ranker.

---

## 13. Evidence Extraction

Evidence extraction converts papers into structured research information.

Conceptual model:

```text
Paper
 │
 ↓
Evidence Extractor
 │
 ├── Study Design
 ├── Population
 ├── Sample Size
 ├── Intervention / Exposure
 ├── Comparator
 ├── Outcomes
 ├── Findings
 └── Limitations
```

Evidence should retain a reference to its source paper.

Example:

```text
EvidenceRecord
    paper_id
    study_design
    population
    sample_size
    intervention
    comparator
    outcomes
    findings
    limitations
```

Missing information should remain missing.

### 13.1 The evidence contract (as implemented)

Defined in `app/schemas/evidence.py`. This is what later slices (comparison, contradiction and gap analysis, cited
synthesis) read, so it is deliberately strict.

**Every field says how we know it** (`FieldStatus`):

| Status | Meaning |
|---|---|
| `stated` | the value is written in the paper's **abstract** |
| `inferred` | not in the abstract, but derivable from metadata (the **title**); only allowed for study design and population |
| `unavailable` | we could not establish it |

`unavailable` means **"not found"**, never **"the paper has none"**: an unavailable `comparator` does not mean "there was
no comparator". Missing information is never treated as negative evidence.

**Validators enforce the rules**, so a malformed record cannot exist: an `unavailable` field must be completely empty; a
`stated` or `inferred` value must cite the text it came from; `stated` may only cite the abstract and `inferred` only
the title; counts must be positive; study design must come from a fixed vocabulary.

**Source traceability.**

* `Evidence.paper_id` is required (the canonical `Paper.id`). Evidence cannot exist without a paper.
* Every value cites a `SourceSpan`: the **verbatim sentence** it was taken from, the abstract section label if the abstract
  was structured (`METHODS`, `RESULTS`...), and **real character offsets** into `Paper.abstract` / `Paper.title` such that
  `paper_text[start:end] == span.text`. Offsets are never invented: a future extractor that cannot say where a span is
  leaves both `None`.
* Values are copied from the text, never paraphrased. Statistics are copied as written (`"p < 0.001"`, `"95% CI 0.2 to 0.8"`)
  and never computed or interpreted.
* `abstract_status` separates `present`, `empty` (supplied but blank) and `missing` (no abstract).

Fields: `study_design`, `population`, `sample_size` (`participants` and `studies`, each stated or null, never summed or
guessed), `intervention`, `comparator`, `outcomes` (a list), `findings` (sentences of results/conclusions, each with its
statistics), `limitations` (explicit statements about the study only).

### 13.2 Extractor interface

```python
class EvidenceExtractor(Protocol):
    name: str
    async def extract(self, paper: Paper) -> Evidence: ...
```

The method is `async` although the first implementation does no I/O, so that a model-backed extractor (a later slice)
can replace it without changing the pipeline. Anything implementing the protocol must obey the contract in 13.1. The
orchestrator also checks that the returned `paper_id` matches the paper it asked about and discards evidence that does not.

### 13.3 The first implementation: rule-based, no model

`RuleBasedEvidenceExtractor` (`app/services/evidence/`) uses explicit phrase patterns over the abstract's sections and
sentences. It is **deliberately conservative**: a missed value is acceptable, a wrong one is not, so anything ambiguous
becomes `unavailable` (for example several competing participant counts, group sizes that would have to be added, or a
count that describes a subset such as "42 completed").

Measured on hand-read abstracts (`backend/evaluation/EVIDENCE_RESULTS.md`): about **73% of fields correct on unseen papers**,
**0 values** absent from their cited text across all 389 snapshot papers, and the weakest fields are `outcomes`,
`comparator` and `intervention`. Deterministic extraction is **insufficient** where abstracts name things indirectly
("a webinar series ... for its feasibility"), define abbreviations ("group C received only SE"), or give counts as
single-digit words ("five trials"). That gap is what the model-backed extractor in the next slice is for.

### 13.4 API design decision: separate collection, not embedded in `Paper`

`POST /research` returns evidence as a **separate collection keyed by paper id**, next to `papers`:

```text
{ "papers": [Paper, ...], "evidence": { "<paper id>": Evidence, ... }, "provider_status": {...}, ... }
```

Why separate, not an `evidence` field on `Paper`:

1. **`Paper` is a provider-neutral retrieval record.** Evidence is derived analysis; embedding it would couple the two,
   and every future analysis (comparison, gaps) would then want a place on `Paper` too.
2. **Evidence has a different lifecycle.** It is produced after ranking, by an extractor that will change (rules, then a
   model), may fail per paper, and may be regenerated without re-retrieving anything.
3. **Failure isolation.** If extraction fails for one paper it is absent from `evidence`; the paper itself is unaffected.
   An embedded field would force a placeholder, and a placeholder looks like data.
4. **Backwards compatible.** Existing consumers of `papers` see no change; `evidence` is an additive key.

The cost is that a client joins the two by id, and that evidence is about 55-60% of the response size (roughly 80-120 KB
per request for ~40 papers). If that matters, the next step is an opt-in or a top-N limit, not embedding.

---

## 14. Research Analysis

Research analysis operates on structured evidence.

```text
                 Evidence
                    │
          ┌─────────┼─────────┐
          ↓         ↓         ↓
     Comparison    Gaps   Contradictions
```

### 14.1 Comparison

Compare studies across:

* Population
* Study design
* Intervention
* Comparator
* Sample size
* Outcomes
* Findings
* Limitations

### 14.2 Gap Detection

Identify apparent limitations in the retrieved literature.

The system must use cautious language.

Example:

```text
Based on the retrieved literature,
few studies examined...
```

Not:

```text
No research exists...
```

unless the evidence genuinely supports that conclusion.

### 14.3 Contradiction Detection

Identify studies with materially different findings.

The system should explain possible differences using available study characteristics rather than inventing explanations.

---

## 15. LLM Provider Layer

The LLM must be isolated behind a provider abstraction.

Conceptually:

```python
class LLMProvider:
    async def generate(...):
        ...
```

This allows the underlying open-weight model to change without requiring changes throughout the application.

The provider layer is responsible for:

* Prompt construction
* Model requests
* Response handling
* Streaming
* Error handling
* Structured output validation where required

---

## 16. Open-weight Model

The project must use an open-weight model.

The model should primarily perform:

* Evidence extraction
* Evidence comparison
* Research synthesis
* Structured analysis

The model should not be treated as the source of truth.

Academic sources are the source of truth.

---

## 17. Citation Grounding

The synthesis pipeline should look like:

```text
Research Question
       ↓
Retrieved Papers
       ↓
Structured Evidence
       ↓
LLM
       ↓
Draft Synthesis
       ↓
Citation Validation
       ↓
Final Response
```

Citation validation should ensure:

* Citation references exist
* Citation references correspond to retrieved papers
* Unsupported source IDs are rejected
* The final answer does not cite papers that were not retrieved

Where practical, important claims should be mapped to supporting evidence before being displayed.

---

## 18. Streaming

The application should support streaming generated responses.

Possible flow:

```text
Frontend
   │
   │ POST /research
   ↓
FastAPI
   ↓
Research Orchestrator
   ↓
Retrieval
   ↓
Evidence
   ↓
LLM
   │
   │ streamed tokens/events
   ↓
Frontend
```

SSE is the preferred initial mechanism.

Streaming should improve perceived latency without making the underlying workflow unnecessarily complicated.

---

## 19. Error Handling

External services are expected to fail occasionally.

The system should handle:

* API timeouts
* Rate limits
* Invalid responses
* Empty search results
* Missing abstracts
* LLM failures
* Provider unavailability

A failure in one academic provider should not necessarily prevent results from another provider from being used.

Example:

```text
PubMed ──────── SUCCESS ───────┐
                               ├──> Research continues
Crossref ────── FAILURE ───────┘
```

---

## 20. Storage

Initial storage should remain simple.

Preferred initial option:

```text
SQLite
```

Potential uses:

* Research sessions
* Cached papers
* Evidence records
* Evaluation data

Persistent storage should not become a prerequisite for the core research pipeline unless necessary.

---

## 21. Deployment

Target deployment:

```text
Render
```

Initial deployment architecture:

```text
User
 │
 ↓
Frontend
 │
 ↓
FastAPI Backend
 │
 ├── PubMed
 ├── Crossref
 └── Open-weight LLM Provider
```

Avoid unnecessary infrastructure.

---

## 22. Low-Latency Strategy

Primary latency optimizations:

```text
Concurrent retrieval
        ↓
Cheap ranking
        ↓
Process only relevant papers
        ↓
Minimize LLM calls
        ↓
Stream final generation
```

Do not optimize prematurely.

Measure latency before introducing additional infrastructure.

---

## 23. Security

Secrets must be stored through environment variables.

Never commit:

```text
API keys
Access tokens
Passwords
Deployment credentials
```

Provider clients should not log secrets.

User-provided research queries should be treated as untrusted input.

---

## 24. Testing Architecture

Testing should exist at multiple levels.

### Unit Tests

Test:

* Query processing
* Provider parsing
* Paper normalization
* Deduplication
* Ranking
* Evidence schema validation
* Citation validation

### Integration Tests

Test:

```text
Query
 ↓
Search
 ↓
Normalize
 ↓
Rank
 ↓
Evidence
```

### End-to-end Tests

Test the complete workflow:

```text
Research Question
 ↓
Academic Search
 ↓
Evidence
 ↓
Analysis
 ↓
LLM
 ↓
Citations
 ↓
Response
```

External APIs should be mocked in deterministic tests where appropriate.

---

## 25. Initial Build Strategy

Build the system incrementally. (Milestones 1-3 are implemented; see "Implementation status" at the top.)

### Milestone 1

```text
Research Question
       ↓
PubMed
       ↓
Normalized Papers
```

### Milestone 2

```text
Research Question
       ↓
PubMed + Crossref
       ↓
Deduplication
       ↓
Ranking
```

### Milestone 3

```text
Ranked Papers
       ↓
Evidence Extraction
       ↓
Structured Evidence
```

### Milestone 4

```text
Structured Evidence
       ↓
Open-weight LLM
       ↓
Citation-grounded Synthesis
```

### Milestone 5

```text
Evidence
   ↓
Comparison
   ↓
Gap Detection
   ↓
Contradiction Detection
```

### Milestone 6

```text
Complete Backend
       ↓
Streaming API
       ↓
Frontend
       ↓
Render
```

---

## 26. Architecture Rules

1. Keep the backend modular.
2. Keep external providers behind interfaces.
3. Keep the LLM behind an abstraction.
4. Keep research logic out of the frontend.
5. Prefer async/concurrent I/O for independent external requests.
6. Validate structured outputs.
7. Preserve source provenance throughout the pipeline.
8. Never fabricate evidence or citations.
9. Prefer graceful degradation over total failure.
10. Avoid unnecessary infrastructure.
11. Do not introduce autonomous agents unless a concrete requirement emerges.
12. Keep the first implementation small enough to understand completely.

The architecture should evolve based on measured requirements, not hypothetical future scale.
