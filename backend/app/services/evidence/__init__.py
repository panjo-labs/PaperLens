"""Evidence extraction: turns retrieved papers into structured, source-cited Evidence."""

from app.services.evidence.base import EvidenceExtractor
from app.services.evidence.rule_based import RuleBasedEvidenceExtractor

__all__ = ["EvidenceExtractor", "RuleBasedEvidenceExtractor"]
