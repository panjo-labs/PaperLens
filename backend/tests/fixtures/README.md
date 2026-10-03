# Fixtures

`efetch_sample.xml` — five real records from the PubMed efetch API (recorded 2026-10-03),
unmodified apart from being combined into one `PubmedArticleSet`:

| PMID     | Covers                                                                 |
|----------|------------------------------------------------------------------------|
| 42806217 | structured abstract (labelled sections), DOI, large reference list     |
| 42802597 | unstructured single-paragraph abstract, DOI                            |
| 42813137 | no DOI anywhere in the record                                          |
| 39306741 | no `<Abstract>` element at all                                         |
| 42602955 | mix of personal authors and a `<CollectiveName>` author                |

`esearch_sample.json` — a real esearch JSON response (shape reference only).
