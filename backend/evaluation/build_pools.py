"""One-off script: snapshot REAL provider results for each evaluation question.

Run this only when you want to refresh the dataset (it uses the live PubMed and Crossref APIs):
    NCBI_EMAIL=you@example.com python evaluation/build_pools.py

The snapshot (`pools.json`) is what makes the evaluation deterministic and offline: the same
papers every time, so a change in ranking scores can only come from the ranking code.
"""

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable

from app.config import Settings  # noqa: E402
from app.providers.crossref import CrossrefProvider  # noqa: E402
from app.providers.pubmed import PubMedProvider  # noqa: E402
from app.services.query_processor import process_question  # noqa: E402

HERE = Path(__file__).resolve().parent


async def main() -> None:
    settings = Settings()  # reads NCBI_EMAIL etc. from the environment / .env
    questions = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
    pools = {"recorded_on": date.today().isoformat(), "questions": {}}

    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        providers = [PubMedProvider(client, settings), CrossrefProvider(client, settings)]
        for item in questions:
            query = process_question(item["question"]).search_query
            entry = {"question": item["question"], "query": query, "providers": {}}
            for provider in providers:  # one at a time: gentle on the public APIs
                papers = await provider.search(query)
                entry["providers"][provider.name] = [p.model_dump() for p in papers]
                print(f"{item['id']} {provider.name:9s} {len(papers):2d} papers  query={query!r}")
            pools["questions"][item["id"]] = entry

    (HERE / "pools.json").write_text(json.dumps(pools, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", HERE / "pools.json")


if __name__ == "__main__":
    asyncio.run(main())
