"""Deduplicator: merges records that describe the same paper (e.g. one from PubMed, one from Crossref).

How two records are decided to be "the same paper" (checked in this order of trust):
  1. Same DOI        (after cleaning: lowercase, no "https://doi.org/" or "doi:" prefix)
  2. Same PubMed ID  ("pubmed:12345678")
  3. Same title      - ONLY for records that have neither a DOI nor a PubMed ID, and only if
                       the title is identical after normalization (lowercase, no accents or
                       punctuation, single spaces). No fuzzy matching.

Why the title rule is so strict: different papers can share a title (for example, every
update of a Cochrane review has the same title but a different DOI). Records with a DOI or
PMID already have a trustworthy identity, so we never let a title merge them.

Merging keeps the most complete information and the full provenance (`source_ids`).
Nothing is invented: a field stays empty if no contributing record has it.

The work is O(n): each record is hashed under its keys (a dict lookup), no pairwise comparison.
"""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence

from app.schemas.paper import Paper

_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)")
_NOT_ALNUM = re.compile(r"[\W_]+")  # any run of punctuation / symbols / whitespace


def normalize_doi(doi: str | None) -> str | None:
    """"HTTPS://doi.org/10.1000/ABC " -> "10.1000/abc". Returns None for empty values."""
    if not doi:
        return None
    cleaned = _DOI_PREFIX.sub("", doi.strip().lower()).strip()
    return cleaned or None


def normalize_title(title: str | None) -> str | None:
    """"Low-Back Pain: A Review!" -> "low back pain a review". Returns None if nothing is left."""
    if not title:
        return None
    # NFKD splits "é" into "e" + an accent mark; we then drop the accent marks.
    decomposed = unicodedata.normalize("NFKD", title)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Turn every run of punctuation into one space, then trim.
    words = _NOT_ALNUM.sub(" ", without_accents.lower()).split()
    return " ".join(words) or None


def _pmids(paper: Paper) -> list[str]:
    """PubMed IDs this paper is known by, read from its provenance ("pubmed:123" -> "123")."""
    return [sid.split(":", 1)[1] for sid in paper.source_ids if sid.startswith("pubmed:")]


def _identity_keys(paper: Paper) -> list[str]:
    """The keys under which this paper can be recognised as a duplicate of another."""
    keys: list[str] = []
    doi = normalize_doi(paper.doi)
    if doi:
        keys.append(f"doi:{doi}")
    pmids = _pmids(paper)
    keys.extend(f"pmid:{pmid}" for pmid in pmids)

    # Title is only a last resort for records with no other identity.
    if not doi and not pmids:
        title = normalize_title(paper.title)
        if title:
            keys.append(f"title:{title}")
    return keys


def deduplicate(papers: Sequence[Paper]) -> list[Paper]:
    """Merge duplicates. Order of the input matters: the FIRST record of a group is the
    "base" whose id/title/source are kept, so callers should list the preferred provider first.
    The output keeps the order in which each paper first appeared.
    """
    # Union-find ("disjoint sets"): `parent[i]` points toward the group leader of record i.
    # Records sharing any key are joined into one group, even through a chain
    # (A shares a DOI with B, B shares a PMID with C -> A, B, C are one paper).
    parent = list(range(len(papers)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]  # shortcut so later lookups are faster
            i = parent[i]
        return i

    first_seen: dict[str, int] = {}  # key -> index of the first record that had it
    for index, paper in enumerate(papers):
        for key in _identity_keys(paper):
            if key in first_seen:
                a, b = find(first_seen[key]), find(index)
                if a != b:
                    # The smaller index becomes the leader, so the earliest record leads.
                    parent[max(a, b)] = min(a, b)
            else:
                first_seen[key] = index

    groups: dict[int, list[Paper]] = defaultdict(list)
    for index, paper in enumerate(papers):
        groups[find(index)].append(paper)

    # dict keeps insertion order, and leaders were inserted in order of first appearance.
    return [_merge(members) for members in groups.values()]


def _merge(group: list[Paper]) -> Paper:
    """Combine records of one paper, keeping the most complete value of each field."""
    base = group[0]
    if len(group) == 1:
        return base

    return base.model_copy(
        update={
            # Provenance: every contributing record, in order, without repeats.
            "source_ids": list(dict.fromkeys(sid for p in group for sid in p.source_ids)),
            # More authors listed = more complete.
            "authors": max((p.authors for p in group), key=len),
            # Longest abstract wins (None if nobody has one).
            "abstract": max((p.abstract for p in group if p.abstract), key=len, default=None),
            # Most precise date wins: "2024-03-15" beats "2024-03" beats "2024".
            "publication_date": max(
                (p.publication_date for p in group if p.publication_date), key=len, default=None
            ),
            # For these, the first record that has a value is used.
            "journal": next((p.journal for p in group if p.journal), None),
            "doi": next((p.doi for p in group if p.doi), None),
            "url": next((p.url for p in group if p.url), None),
            # Each provider's original position is kept; if one provider listed the paper twice,
            # its better (lower) position is used.
            "provider_ranks": _merge_provider_ranks(group),
            # A score from an earlier ranking would be stale for the merged paper.
            "rank_score": None,
        }
    )


def _merge_provider_ranks(group: list[Paper]) -> dict[str, int]:
    """Combine {"pubmed": 3} and {"crossref": 12} into {"pubmed": 3, "crossref": 12}."""
    merged: dict[str, int] = {}
    for paper in group:
        for provider, rank in paper.provider_ranks.items():
            # keep the best (smallest) position seen for that provider
            merged[provider] = min(rank, merged.get(provider, rank))
    return merged
