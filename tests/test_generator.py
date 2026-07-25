from generator import GeneratedPost, validate_post_payload


def make_payload(*, comment: str, insert_used: str = "наконец-то", news: str | None = None) -> GeneratedPost:
    long_news = news or (
        "Флориан Виртц близок к переходу в Ливерпуль за 130 млн евро. "
        "Сделка находится на стадии medical."
    )
    return GeneratedPost(
        news=long_news,
        reliability_note="Источник силён по этому рынку, а стадия сделки уже почти финальная.",
        comment=comment,
        insert_used=insert_used,
    )


def test_validation_accepts_valid_payload() -> None:
    payload = make_payload(
        comment=(
            "Наконец-то клуб берёт игрока под старт, а не в ротацию. "
            "Для позиционной атаки это дорогая, но логичная ставка."
        )
    )
    problems, text = validate_post_payload(
        payload,
        stars=4,
        recent_inserts=["к сожалению"],
        source_name="BBC Sport Football",
        source_url="https://example.com/news",
        source_tier="tier1",
    )
    assert problems == []
    assert 240 <= len(text) <= 420
    assert text.startswith("🔵 ")


def test_validation_rejects_recent_insert_reuse() -> None:
    payload = make_payload(
        comment=(
            "Наконец-то клуб закрывает позицию без лишнего торга. "
            "Решение дорогое, но спортивная логика у него есть."
        )
    )
    problems, _ = validate_post_payload(
        payload,
        stars=4,
        recent_inserts=["наконец-то"],
        source_name="BBC Sport Football",
        source_url="https://example.com/news",
        source_tier="tier1",
    )
    assert "insert_used уже был в recent_inserts" in problems


def test_validation_rejects_skepticism_with_five_stars() -> None:
    payload = make_payload(
        comment=(
            "Верится с трудом, хотя формально источник сильный и сумма уже названа. "
            "Сам переход выглядит спорно для структуры состава."
        ),
        insert_used="верится с трудом",
    )
    problems, _ = validate_post_payload(
        payload,
        stars=5,
        recent_inserts=[],
        source_name="BBC Sport Football",
        source_url="https://example.com/news",
        source_tier="tier1",
    )
    assert "скепсис запрещён при 5 звёздах" in problems


def test_validation_rejects_banned_phrase() -> None:
    payload = make_payload(
        comment=(
            "Наконец-то состав получает нужный профиль. Толпа видит громкое имя, но алгоритм видит правильную роль в структуре."
        )
    )
    problems, _ = validate_post_payload(
        payload,
        stars=4,
        recent_inserts=[],
        source_name="BBC Sport Football",
        source_url="https://example.com/news",
        source_tier="tier1",
    )
    assert any(problem.startswith("запрещённая фраза") for problem in problems)


def test_validation_rejects_duplicate_comment() -> None:
    duplicate = (
        "Флориан Виртц близок к переходу в Ливерпуль за 130 млн евро. "
        "Сделка находится на стадии medical."
    )
    payload = make_payload(
        news=duplicate,
        comment=f"Неожиданно, {duplicate[0].lower()}{duplicate[1:]}",
        insert_used="неожиданно",
    )
    problems, _ = validate_post_payload(
        payload,
        stars=4,
        recent_inserts=[],
        source_name="BBC Sport Football",
        source_url="https://example.com/news",
        source_tier="tier1",
    )
    assert "comment дублирует news" in problems
