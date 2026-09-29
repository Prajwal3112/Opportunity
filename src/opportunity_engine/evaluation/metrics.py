"""Metrics from manually labelled real records; no quality claim without labels."""
from __future__ import annotations

from pydantic import BaseModel

from opportunity_engine.domain.models import EvaluationLabel, RelevanceTier


class EvaluationMetrics(BaseModel):
    precision: float | None
    recall: float | None
    false_positive_rate: float | None
    false_negative_rate: float | None
    labelled_count: int


def calculate_metrics(labels_and_tiers: list[tuple[EvaluationLabel, RelevanceTier]]) -> EvaluationMetrics:
    relevant = {EvaluationLabel.RELEVANT}
    predicted_positive = {RelevanceTier.HIGH, RelevanceTier.MEDIUM}
    pairs = [(label, tier) for label, tier in labels_and_tiers if label != EvaluationLabel.UNCERTAIN]
    tp = sum(label in relevant and tier in predicted_positive for label, tier in pairs)
    fp = sum(label not in relevant and tier in predicted_positive for label, tier in pairs)
    fn = sum(label in relevant and tier not in predicted_positive for label, tier in pairs)
    tn = sum(label not in relevant and tier not in predicted_positive for label, tier in pairs)
    return EvaluationMetrics(
        precision=None if tp + fp == 0 else tp / (tp + fp),
        recall=None if tp + fn == 0 else tp / (tp + fn),
        false_positive_rate=None if fp + tn == 0 else fp / (fp + tn),
        false_negative_rate=None if fn + tp == 0 else fn / (fn + tp),
        labelled_count=len(pairs),
    )
