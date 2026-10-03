"""Guards against dedup/ranking accidentally becoming quadratic (slow on big inputs).

Timing tests can be noisy, so each uses the best of several runs and generous limits. A genuinely
quadratic implementation (4x the papers -> ~16x the time) still fails them clearly.
"""

import random
import time

from app.schemas.paper import Paper
from app.services.deduplicator import deduplicate
from app.services.ranker import rank_papers

QUESTION = "effectiveness of exercise therapy for chronic low back pain"
VOCAB = "exercise therapy chronic low back pain study patients trial review outcome treatment".split()


def make_papers(n: int, duplicate_every: int = 4) -> list[Paper]:
    """n records, of which every `duplicate_every`-th repeats an earlier DOI (like a cross-provider overlap)."""
    rng = random.Random(0)
    papers = []
    for i in range(n):
        doi_index = i - 1 if i % duplicate_every == 0 and i else i
        words = " ".join(rng.choice(VOCAB) for _ in range(120))
        papers.append(
            Paper(
                id=f"x:{i}", title=" ".join(rng.choice(VOCAB) for _ in range(10)),
                authors=["A B"], abstract=words, publication_date=f"20{i % 25:02d}",
                doi=f"10.1000/{doi_index}", source="x", source_id=str(i),
            )
        )
    return papers


def best_of(runs: int, fn) -> float:
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return min(times)


def test_normal_result_set_is_processed_in_a_few_milliseconds():
    # A real request returns ~40 raw papers (20 per provider).
    papers = make_papers(40)
    elapsed = best_of(5, lambda: rank_papers(QUESTION, deduplicate(papers)))
    assert elapsed < 0.1, f"dedup+rank of 40 papers took {elapsed * 1000:.1f} ms"


def test_a_large_result_set_is_still_fast():
    papers = make_papers(2_000)
    elapsed = best_of(3, lambda: rank_papers(QUESTION, deduplicate(papers)))
    assert elapsed < 3.0, f"dedup+rank of 2000 papers took {elapsed:.2f} s"


def test_deduplication_time_grows_roughly_linearly():
    small, large = make_papers(2_000), make_papers(8_000)  # 4x the data
    t_small = best_of(3, lambda: deduplicate(small))
    t_large = best_of(3, lambda: deduplicate(large))
    # Linear -> ~4x. Quadratic -> ~16x. Allow a lot of slack for timer noise.
    assert t_large < max(t_small, 0.005) * 10, f"{t_small:.4f}s -> {t_large:.4f}s"


def test_ranking_time_grows_roughly_linearly():
    small, large = make_papers(500), make_papers(2_000)
    t_small = best_of(3, lambda: rank_papers(QUESTION, small))
    t_large = best_of(3, lambda: rank_papers(QUESTION, large))
    assert t_large < max(t_small, 0.005) * 10, f"{t_small:.4f}s -> {t_large:.4f}s"


def test_many_duplicates_of_one_paper_do_not_blow_up():
    # Worst case for a naive pairwise comparison: every record is the same paper.
    papers = [
        Paper(id=f"x:{i}", title="Same", authors=[], doi="10.1/same", source="x", source_id=str(i))
        for i in range(5_000)
    ]
    start = time.perf_counter()
    result = deduplicate(papers)
    assert len(result) == 1 and len(result[0].source_ids) == 5_000
    assert time.perf_counter() - start < 2.0
