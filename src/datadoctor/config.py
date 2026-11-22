"""Per-instance settings, loaded from a TOML file (see config/)."""

import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    more: str  # data element uid that should be >= `less`
    less: str
    why: str


@dataclass(frozen=True)
class Config:
    name: str
    district_level: int
    facility_level: int
    datasets: list[str]
    watch: list[str]
    rules: list[Rule]


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    try:
        return Config(
            name=raw["name"],
            district_level=int(raw["district_level"]),
            facility_level=int(raw["facility_level"]),
            datasets=list(raw["datasets"]),
            watch=list(raw["watch"]),
            rules=[Rule(r["more"], r["less"], r.get("why", "")) for r in raw.get("rules", [])],
        )
    except KeyError as e:
        raise ValueError(f"{path}: missing required setting {e}") from e
