# Backend (milestone 1)

Research question → query normalization → PubMed esearch → efetch → `Paper[]` → JSON.

```bash
python -m venv .venv && .venv/Scripts/activate      # Python 3.11+
pip install -e ".[dev]"
cp .env.example .env                                 # set NCBI_EMAIL (and optionally NCBI_API_KEY)
uvicorn app.main:app --reload
pytest
```

`POST /research` with `{"question": "..."}` returns `question`, `queries_used`, `papers`
and `provider_status`. Status codes: 200 (including zero results and partial provider failure),
422 (invalid question), 502 (every provider failed; same body shape).
