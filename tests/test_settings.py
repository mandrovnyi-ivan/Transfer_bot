from settings import Settings


def test_allowed_users_falls_back_to_owner_id() -> None:
    settings = Settings.model_construct(
        owner_id=123,
        allowed_users_raw="",
    )
    assert settings.allowed_user_ids == [123]


def test_allowed_users_parses_csv_and_deduplicates() -> None:
    settings = Settings.model_construct(
        owner_id=123,
        allowed_users_raw="8774768397, 8613038789, 8774768397",
    )
    assert settings.allowed_user_ids == [8774768397, 8613038789]
