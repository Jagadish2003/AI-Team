from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol
from uuid import UUID, uuid4


PRIMARY_METRIC_NAME = "metric_value"


class DetectorEvaluationLike(Protocol):
    detector_id: str
    detector_cls: type
    signal_source: str
    metric_value: float
    threshold: Optional[float]
    fired: bool
    raw_evidence: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class SignalSnapshot:
    id: UUID = field(default_factory=uuid4)
    org_id: str
    run_id: str
    pack_id: str
    detector_id: str
    signal_key: str
    metric_name: str
    metric_value: float
    threshold: Optional[float]
    fired: bool
    signal_source: str
    captured_at: datetime
    baseline_mean: Optional[float] = None
    baseline_stddev: Optional[float] = None
    baseline_window_days: Optional[int] = None
    baseline_calculated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", self._coerce_uuid(self.id))
        self._validate_non_empty("org_id", self.org_id)
        self._validate_non_empty("run_id", self.run_id)
        self._validate_non_empty("pack_id", self.pack_id)
        self._validate_non_empty("detector_id", self.detector_id)
        self._validate_non_empty("signal_key", self.signal_key)
        self._validate_non_empty("metric_name", self.metric_name)
        self._validate_non_empty("signal_source", self.signal_source)

        self._validate_number("metric_value", self.metric_value)
        self._validate_optional_number("threshold", self.threshold)
        self._validate_optional_number("baseline_mean", self.baseline_mean)
        self._validate_optional_number("baseline_stddev", self.baseline_stddev)

        if not isinstance(self.fired, bool):
            raise ValueError("fired must be a boolean")
        if not isinstance(self.captured_at, datetime):
            raise ValueError("captured_at must be a datetime")
        if self.baseline_calculated_at is not None and not isinstance(
            self.baseline_calculated_at, datetime
        ):
            raise ValueError("baseline_calculated_at must be a datetime or None")
        if self.baseline_window_days is not None:
            if isinstance(self.baseline_window_days, bool) or not isinstance(
                self.baseline_window_days, int
            ):
                raise ValueError("baseline_window_days must be an integer or None")

    @classmethod
    def from_primary_metric(
        cls,
        *,
        org_id: str,
        run_id: str,
        pack_id: str,
        evaluation: DetectorEvaluationLike,
        run_completed_at: datetime,
        id: UUID | str | None = None,
    ) -> SignalSnapshot:
        return cls(
            id=cls._coerce_uuid(id) if id is not None else uuid4(),
            org_id=org_id,
            run_id=run_id,
            pack_id=pack_id,
            detector_id=evaluation.detector_id,
            signal_key=build_signal_key(
                pack_id, evaluation.detector_id, PRIMARY_METRIC_NAME
            ),
            metric_name=PRIMARY_METRIC_NAME,
            metric_value=float(evaluation.metric_value),
            threshold=(
                None if evaluation.threshold is None else float(evaluation.threshold)
            ),
            fired=bool(evaluation.fired),
            signal_source=evaluation.signal_source,
            captured_at=run_completed_at,
        )

    @classmethod
    def from_signal_metric(
        cls,
        *,
        org_id: str,
        run_id: str,
        pack_id: str,
        evaluation: DetectorEvaluationLike,
        metric_name: str,
        run_completed_at: datetime,
        id: UUID | str | None = None,
    ) -> SignalSnapshot:
        raw_value = evaluation.raw_evidence.get(metric_name)
        if not is_signal_metric_value(raw_value):
            raise ValueError(f"raw_evidence[{metric_name!r}] must be numeric")
        return cls(
            id=cls._coerce_uuid(id) if id is not None else uuid4(),
            org_id=org_id,
            run_id=run_id,
            pack_id=pack_id,
            detector_id=evaluation.detector_id,
            signal_key=build_signal_key(pack_id, evaluation.detector_id, metric_name),
            metric_name=metric_name,
            metric_value=float(raw_value),
            threshold=None,
            fired=False,
            signal_source=evaluation.signal_source,
            captured_at=run_completed_at,
        )

    @classmethod
    def from_signal_metrics(
        cls,
        *,
        org_id: str,
        run_id: str,
        pack_id: str,
        evaluation: DetectorEvaluationLike,
        run_completed_at: datetime,
    ) -> list[SignalSnapshot]:
        snapshots: list[SignalSnapshot] = []
        for metric_name in getattr(evaluation.detector_cls, "SIGNAL_METRICS", []):
            raw_value = evaluation.raw_evidence.get(metric_name)
            if is_signal_metric_value(raw_value):
                snapshots.append(
                    cls.from_signal_metric(
                        org_id=org_id,
                        run_id=run_id,
                        pack_id=pack_id,
                        evaluation=evaluation,
                        metric_name=metric_name,
                        run_completed_at=run_completed_at,
                    )
                )
        return snapshots

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "org_id": self.org_id,
            "run_id": self.run_id,
            "pack_id": self.pack_id,
            "detector_id": self.detector_id,
            "signal_key": self.signal_key,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "threshold": self.threshold,
            "fired": self.fired,
            "signal_source": self.signal_source,
            "captured_at": self.captured_at,
            "baseline_mean": self.baseline_mean,
            "baseline_stddev": self.baseline_stddev,
            "baseline_window_days": self.baseline_window_days,
            "baseline_calculated_at": self.baseline_calculated_at,
        }

    @staticmethod
    def _coerce_uuid(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("id must be a UUID") from exc

    @staticmethod
    def _validate_non_empty(field_name: str, value: str) -> None:
        if value is None or not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} is required")

    @staticmethod
    def _validate_number(field_name: str, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field_name} must be numeric")

    @classmethod
    def _validate_optional_number(cls, field_name: str, value: Optional[float]) -> None:
        if value is not None:
            cls._validate_number(field_name, value)


def build_signal_key(pack_id: str, detector_id: str, metric_name: str) -> str:
    return f"{pack_id}::{detector_id}::{metric_name}"


def is_signal_metric_value(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))
