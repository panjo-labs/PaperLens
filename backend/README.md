# Backend (milestone 1)

Research question → query normalization → PubMed **and** Crossref (searched concurrently) → combined
`Paper[]` → JSON. Papers are returned PubMed first, then Crossref, each in its provider's own relevance
order. There is no deduplication or ranking yet, so the same paper can appear from both sources.

```bash
python -m venv .venv && .venv/Scripts/activate      # Python 3.11+
pip install -e ".[dev]"
cp .env.example .env     # set NCBI_EMAIL; optionally NCBI_API_KEY and CROSSREF_MAILTO (faster Crossref pool)
uvicorn app.main:app --reload
pytest
```

`POST /research` with `{"question": "..."}` returns `question`, `queries_used`, `papers`
and `provider_status`. Status codes: 200 (including zero results and partial provider failure),
422 (invalid question), 502 (every provider failed; same body shape). `provider_status` has one entry per
provider (`pubmed`, `crossref`), so one provider failing never hides the other's papers.
