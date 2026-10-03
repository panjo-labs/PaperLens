# Backend (milestone 1)

Research question → query normalization → PubMed **and** Crossref (searched concurrently) →
**deduplicate** → **rank** → `Paper[]` → JSON.

* **Deduplication** (`app/services/deduplicator.py`): the same paper from both providers is merged by DOI, then
  PubMed ID, then (only for records with neither) exact normalized title. The merged paper keeps the most complete
  metadata and lists every contributing record in `source_ids`, e.g. `["pubmed:123", "crossref:10.1000/x"]`.
* **Ranking** (`app/services/ranker.py`): a documented word-overlap score (title weighted 3x more than abstract).
  Each paper gets `rank_score` (0-1). It is a retrieval heuristic, **not** a measure of scientific relevance.
* **Evaluation**: `evaluation/` holds a small offline benchmark; see `evaluation/README.md` for the method,
  results and limits.

```bash
python -m venv .venv && .venv/Scripts/activate      # Python 3.11+
pip install -e ".[dev]"
cp .env.example .env     # set NCBI_EMAIL; optionally NCBI_API_KEY and CROSSREF_MAILTO (faster Crossref pool)
uvicorn app.main:app --reload
pytest
```

`POST /research` with `{"question": "..."}` returns `question`, `queries_used`, `papers` (unique, best match
first) and `provider_status`. `provider_status[...].paper_count` is what each provider *returned*, before duplicates
were removed. Status codes: 200 (including zero results and partial provider failure),
422 (invalid question), 502 (every provider failed; same body shape). `provider_status` has one entry per
provider (`pubmed`, `crossref`), so one provider failing never hides the other's papers.
