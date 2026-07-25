from __future__ import annotations

from typing import Final


BASE_STARS: Final[dict[str, int]] = {
    "official": 5,
    "tier1": 4,
    "tier2": 3,
    "yellow": 2,
    "rumor": 1,
}

POSITIVE_TYPES: Final[set[str]] = {"official", "medical", "here_we_go"}
NEGATIVE_TYPES: Final[set[str]] = {"interest", "rumor"}


def calculate_reliability(source_tier: str, news_type: str) -> tuple[int, list[str]]:
    stars = BASE_STARS.get(source_tier, 1)
    reasons = [f"base:{source_tier}={stars}"]

    if news_type in POSITIVE_TYPES:
        stars += 1
        reasons.append(f"modifier:+1:{news_type}")
    elif news_type in NEGATIVE_TYPES:
        stars -= 1
        reasons.append(f"modifier:-1:{news_type}")
    else:
        reasons.append(f"modifier:0:{news_type}")

    return max(1, min(5, stars)), reasons
