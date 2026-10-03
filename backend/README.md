# Backend

Research question → query normalization → PubMed **and** Crossref (searched concurrently) →
**deduplicate** → **rank** → **extract evidence** → JSON.

* **Deduplication** (`app/services/deduplicator.py`): the same paper from both providers is merged by DOI, then
  PubMed ID, then (only for records with neither) exact normalized title. The merged paper keeps the most complete
  metadata and lists every contributing record in `source_ids`, e.g. `["pubmed:123", "crossref:10.1000/x"]`.
* **Ranking** (`app/services/ranker.py`): a documented word-overlap score (title weighted 3x more than abstract).
  Each paper gets `rank_score` (0-1). It is a retrieval heuristic, **not** a measure of scientific relevance.
* **Evidence extraction** (`app/services/evidence/`): each ranked paper is turned into structured `Evidence`
  (study design, population, sample size, intervention, comparator, outcomes, findings, limitations) by a
  deterministic, rule-based extractor (no LLM). Every value says whether it is `stated` (in the abstract),
  `inferred` (from the title) or `unavailable`, and cites the exact sentence and character offsets it came from.
  A missing value is never guessed. Evidence is returned as a separate `evidence` collection keyed by paper id, not
  inside each paper (see `ARCHITECTURE.md`, section 13).
* **Evaluation**: `evaluation/` holds offline benchmarks for retrieval (`README.md`, `RESULTS.md`) and for evidence
  extraction (`EVIDENCE_RESULTS.md`), including their method and limits.

```bash
python -m venv .venv && .venv/Scripts/activate      # Python 3.11+
pip install -e ".[dev]"
cp .env.example .env     # set NCBI_EMAIL; optionally NCBI_API_KEY and CROSSREF_MAILTO (faster Crossref pool)
uvicorn app.main:app --reload
pytest
```

`POST /research` with `{"question": "..."}` returns `question`, `queries_used`, `papers` (unique, best match
first), `provider_status` and `evidence` (one `Evidence` per paper, keyed by paper id, in the same order). `provider_status[...].paper_count` is what each provider *returned*, before duplicates
were removed. Status codes: 200 (including zero results and partial provider failure),
422 (invalid question), 502 (every provider failed; same body shape). `provider_status` has one entry per
provider (`pubmed`, `crossref`), so one provider failing never hides the other's papers.
