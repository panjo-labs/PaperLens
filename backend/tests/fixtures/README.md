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

`crossref_sample.json` — six real records from the Crossref `/works` API (recorded 2026-10-03,
query "effectiveness exercise therapy chronic low back pain", same `select`/`filter` the provider
sends), unmodified apart from keeping a subset of `items`:

| DOI                               | Covers                                                          |
|-----------------------------------|-----------------------------------------------------------------|
| 10.5348/100041d05pa2018ra         | JATS abstract (`<jats:p>` with "Aims:" label), 3 authors, full date |
| 10.29011/2576-957x.100028         | no `author`, no `abstract`, `&amp;` in journal title, year-only date |
| 10.36283/pjr.zu.14.2/004          | multi-paragraph "Background:" abstract                          |
| 10.1016/j.explore.2022.08.012     | year-month date, no abstract                                    |
| 10.1211/fact.14.2.0019            | no `author`, year-month date                                    |
| 10.1922/pjs.16.1s.2026.518        | authors with a family name only (no given name)                 |
