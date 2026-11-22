"""Pure data quality checks. No I/O: everything here is testable with plain lists."""

from dataclasses import dataclass
from statistics import median


def outlier_score(history: list[float], value: float) -> float:
    """Modified z-score of `value` against `history` (robust to earlier outliers)."""
    mid = median(history)
    deviation = abs(value - mid)
    mad = median(abs(x - mid) for x in history)
    if mad:
        return 0.6745 * deviation / mad
    # Over half the months are identical, so MAD is 0: fall back to mean absolute deviation.
    mean_ad = sum(abs(x - mid) for x in history) / len(history)
    if mean_ad:
        return deviation / (1.2533 * mean_ad)
    return 0.0 if deviation == 0 else float("inf")


@dataclass(frozen=True)
class Completeness:
    rate: float | None  # share of expected reports received; None when nothing was expected
    timely_rate: float | None
    missing: list[str]  # facility ids, sorted
    late: list[str]


def completeness(expected: set[str], reported: set[str], on_time: set[str]) -> Completeness:
    received = reported & expected
    timely = on_time & expected
    n = len(expected)
    return Completeness(
        rate=len(received) / n if n else None,
        timely_rate=len(timely) / n if n else None,
        missing=sorted(expected - received),
        late=sorted(received - timely),
    )


def consistency_violations(
    values: dict[tuple[str, str], float], rules: list[tuple[str, str]]
) -> list[tuple[str, str, str, float, float]]:
    """For each rule (a, b) meaning "a >= b", list (facility, a, b, a_value, b_value) where it breaks."""
    facilities = sorted({ou for ou, _ in values})
    out = []
    for a, b in rules:
        for ou in facilities:
            va, vb = values.get((ou, a), 0), values.get((ou, b), 0)
            if va < vb:
                out.append((ou, a, b, va, vb))
    return out


def trend_ratio(history: list[float], value: float) -> float | None:
    """This period divided by the average of earlier periods (WHO DQR "consistency over time")."""
    if not history or not sum(history):
        return None
    return value / (sum(history) / len(history))
