from reliability import calculate_reliability


def test_official_is_clamped_to_five() -> None:
    stars, reasons = calculate_reliability("official", "official")
    assert stars == 5
    assert "modifier:+1:official" in reasons


def test_tier1_medical_gets_bonus() -> None:
    stars, reasons = calculate_reliability("tier1", "medical")
    assert stars == 5
    assert "modifier:+1:medical" in reasons


def test_tier2_interest_gets_penalty() -> None:
    stars, reasons = calculate_reliability("tier2", "interest")
    assert stars == 2
    assert "modifier:-1:interest" in reasons


def test_yellow_rumor_is_clamped_to_one() -> None:
    stars, reasons = calculate_reliability("yellow", "rumor")
    assert stars == 1
    assert "modifier:-1:rumor" in reasons


def test_unknown_tier_without_modifier_defaults_to_one() -> None:
    stars, reasons = calculate_reliability("unknown", "talks")
    assert stars == 1
    assert "modifier:0:talks" in reasons
