# TODO

Design lives in `ARCHITECTURE.md`; requirements in `PROJECT_SPEC.md`; measured results in `backend/evaluation/`.

## Done

- [x] **Slice 1** - question -> query normalization -> PubMed (`esearch` + `efetch`) -> normalized `Paper[]`
- [x] **Slice 2A** - Crossref provider; both providers searched concurrently; one provider failing never discards the other's papers
- [x] **Slice 2B** - deduplication (DOI -> PMID -> exact title) and lexical ranking; offline retrieval evaluation
- [x] **Provider rank preserved** - `Paper.provider_ranks`, kept through combine -> dedup -> rank (not used for ordering yet)
- [x] **Slice 3** - structured evidence extraction: strict `Evidence` schema, `EvidenceExtractor` interface, deterministic
      rule-based extractor, `evidence` collection on `POST /research`, hand-checked evaluation (dev + holdout)

## Next

- [ ] **Slice 4** - LLM provider abstraction (open-weight model), model-backed `EvidenceExtractor` that fills what rules cannot,
      citation-grounded synthesis, citation validation (cite only retrieved papers; reject unknown ids)
- [ ] Study comparison, contradiction detection, research-gap detection (spec section 4.8 / 5), on top of `Evidence`
- [ ] SSE streaming of the generated answer; React/Next.js frontend; Render deployment
- [ ] Final evaluation across the whole pipeline (spec section 12): time to first streamed token, groundedness, citation correctness

## Known issues and decisions to revisit

### Verification and deployment
- [ ] **Python 3.11 is untested.** `pyproject.toml` requires `>=3.11` but only 3.10 has been available locally, so every test run so far
      was on 3.10. Install 3.11+ and run the suite before relying on the pin.
- [ ] No deployment config exists (`render.yaml` / `.python-version`). Add when preparing Render; keep it consistent with `pyproject.toml`.
- [ ] Timing-based tests have only run on Windows (coarse timers); run them on Linux before trusting the margins.

### Ranking (`app/services/ranker.py`)
- [ ] On the 10-question evaluation the lexical ranker performs about the same as keeping PubMed's own order
      (recall@10 0.48 vs 0.43; recall@20 0.64 vs 0.74). Treat it as a baseline.
- [ ] Ideas, to be tried only against a larger judged set with some questions held out: down-weight generic words
      ("effect", "improve"), fuse our score with `provider_ranks` (reciprocal rank fusion).
- [ ] Judged data is one reviewer's reading and skewed toward PubMed (59 of 80 relevant papers); have a second person review `judgments.json`.

### Deduplication
- [ ] Two records of one paper with *different* DOIs are not merged (seen: a journal that re-registered its DOIs). A title match never
      overrides conflicting DOIs because Cochrane review versions share titles; a safe rule for the exception is still open.

### Evidence extraction (`app/services/evidence/`)
- [ ] **Honest accuracy is about 73% of fields on unseen papers** (47/64 on the holdout before any fix to it; see `EVIDENCE_RESULTS.md`).
      Later numbers in that file were measured on papers the rules were tuned on and are optimistic. Create a **fresh unseen set** before quoting a new figure.
- [ ] Weakest fields: `outcomes`, `comparator`, `intervention` (abstracts often name them indirectly or via abbreviations).
- [ ] Single-digit number words ("five trials") are not read; larger number words are.
- [ ] Design label from the abstract alone can be less specific than the truth (a title may say "meta-analysis" when the abstract never does).
- [ ] About a third of papers have **no abstract** (126 of 389 unique snapshot papers, 58 of 152 in a live run), so their evidence is mostly
      `unavailable`. Crossref rarely supplies abstracts; consider fetching missing abstracts from PubMed by DOI.
- [ ] **Response size:** evidence is about 55-60% of the `/research` response (80-120 KB for ~40 papers). Consider an opt-in flag or a top-N limit.
- [ ] **Latency:** extraction adds roughly 100-150 ms per request (about 3-5 ms per paper) and runs on the event loop, blocking other requests
      for that time. Fine for now; move to a worker thread or limit to the top-N papers if concurrent load makes it matter.
- [ ] `limitations` is rarely available (about 10% of abstracts); abstracts seldom state study limitations.

### Repository hygiene
- [ ] Add a `.gitattributes` to settle line endings (git warns LF -> CRLF on Windows).
- [ ] `backend/evaluation/pools.json` is about 800 KB; decide whether snapshots belong in the repo.
- [ ] Starlette's `TestClient` warns it will require `httpx2` in future.

### Query processing
- [ ] Quotes, brackets and field tags are stripped, so users cannot supply PubMed phrase/field syntax; lowercase `or`/`not` stay as literal words.
