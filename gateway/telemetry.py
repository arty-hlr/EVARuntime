"""Histogrammes opérationnels en mémoire, bornés et sans contenu utilisateur."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass


MAX_SERIES_PER_HISTOGRAM = 512
_OUTCOMES = {"success", "error", "cancelled", "timeout", "full", "other"}
_OVERFLOW = "__overflow__"


@dataclass(frozen=True)
class HistogramSeries:
    model: str
    node: str
    outcome: str
    buckets: tuple[int, ...]
    count: int
    total: float


@dataclass(frozen=True)
class HistogramSnapshot:
    name: str
    help_text: str
    boundaries: tuple[float, ...]
    series: tuple[HistogramSeries, ...]


class _MutableSeries:
    def __init__(self, bucket_count: int) -> None:
        self.buckets = [0] * bucket_count
        self.count = 0
        self.total = 0.0


class BoundedHistogram:
    """Histogramme à buckets fixes dont le nombre de séries ne peut pas dériver."""

    def __init__(
        self,
        name: str,
        help_text: str,
        boundaries: tuple[float, ...],
        *,
        max_series: int = MAX_SERIES_PER_HISTOGRAM,
    ) -> None:
        if not boundaries or tuple(sorted(boundaries)) != boundaries:
            raise ValueError("les bornes d'histogramme doivent être triées")
        if max_series < 1:
            raise ValueError("max_series doit être >= 1")
        self.name = name
        self.help_text = help_text
        self.boundaries = boundaries
        self.max_series = max_series
        self._series: dict[tuple[str, str, str], _MutableSeries] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _label(value: str, fallback: str) -> str:
        normalized = str(value or fallback).strip()
        return (normalized or fallback)[:128]

    def observe(
        self,
        seconds: float,
        *,
        model: str,
        node: str = "local",
        outcome: str = "success",
    ) -> None:
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            return
        normalized_outcome = outcome if outcome in _OUTCOMES else "other"
        key = (
            self._label(model, "unknown"),
            self._label(node, "local"),
            normalized_outcome,
        )
        with self._lock:
            # Réserver une série unique aux labels excédentaires afin que
            # ``max_series`` reste une borne stricte, overflow inclus.
            if key not in self._series and len(self._series) >= self.max_series - 1:
                key = (_OVERFLOW, _OVERFLOW, "other")
            series = self._series.setdefault(key, _MutableSeries(len(self.boundaries)))
            for index, boundary in enumerate(self.boundaries):
                if value <= boundary:
                    series.buckets[index] += 1
                    break
            series.count += 1
            series.total += value

    def snapshot(self) -> HistogramSnapshot:
        with self._lock:
            series = tuple(
                HistogramSeries(
                    model=model,
                    node=node,
                    outcome=outcome,
                    buckets=tuple(values.buckets),
                    count=values.count,
                    total=values.total,
                )
                for (model, node, outcome), values in sorted(self._series.items())
            )
        return HistogramSnapshot(self.name, self.help_text, self.boundaries, series)

    def reset(self) -> None:
        """Réinitialisation réservée aux tests."""
        with self._lock:
            self._series.clear()


TTFT_SECONDS = BoundedHistogram(
    "eva_inference_ttft_seconds",
    "Temps visible client jusqu'au premier contenu SSE.",
    (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
MODEL_LOAD_SECONDS = BoundedHistogram(
    "eva_model_load_seconds",
    "Durée de chargement réel d'un modèle llama-server.",
    (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0),
)
CAPACITY_QUEUE_SECONDS = BoundedHistogram(
    "eva_capacity_queue_wait_seconds",
    "Durée d'attente dans la queue de capacité locale.",
    (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)


def snapshots() -> tuple[HistogramSnapshot, ...]:
    return (
        TTFT_SECONDS.snapshot(),
        MODEL_LOAD_SECONDS.snapshot(),
        CAPACITY_QUEUE_SECONDS.snapshot(),
    )


def reset_all() -> None:
    for histogram in (TTFT_SECONDS, MODEL_LOAD_SECONDS, CAPACITY_QUEUE_SECONDS):
        histogram.reset()
