from typing import Protocol

from app.schemas.evidence import Evidence
from app.schemas.paper import Paper


class EvidenceExtractor(Protocol):
    """The contract for anything that turns a Paper into structured Evidence.

    Today the only implementation is rule-based (no model involved). A model-backed extractor can
    be added later without changing the pipeline, because it only has to honour this interface and
    the rules in `app.schemas.evidence` (cite your source text, never fill a field you cannot
    support).

    `extract` is async even though the rule-based version does no I/O: a model-backed extractor
    will need to await a network call, and making the method async now avoids a breaking change.
    """

    name: str  # recorded in every Evidence, e.g. "rule-based-v1"

    async def extract(self, paper: Paper) -> Evidence:
        """Return Evidence whose `paper_id` is `paper.id`. Fields that cannot be established stay unavailable."""
        ...
