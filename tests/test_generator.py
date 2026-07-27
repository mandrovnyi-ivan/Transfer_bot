from generator import (
    apply_insert_to_comment,
    build_short_post_text,
    validate_comment_text,
    validate_news_text,
)


def test_validate_news_accepts_compact_factual_block() -> None:
    news = (
        "Флориан Виртц близок к переходу в Ливерпуль за 130 млн евро. "
        "Сделка перешла в стадию медосмотра после финального согласования условий."
    )
    assert validate_news_text(news) == []


def test_validate_news_rejects_short_text() -> None:
    problems = validate_news_text("Виртц близок к переходу.")
    assert "длина news вне диапазона 80–600" in problems


def test_build_short_post_text_without_comment() -> None:
    text = build_short_post_text(
        source_name="Plettigoal",
        source_url="https://x.com/Plettigoal/status/123",
        source_tier="tier1",
        stars=4,
        news="Оттавио близок к переходу в ПСЖ после согласования суммы и личных условий.",
    )
    assert text.startswith("🔵 ")
    assert "Надёжность: ★★★★☆" in text
    assert '📡 <a href="https://x.com/Plettigoal/status/123">Plettigoal</a>' in text
    assert "💬" not in text


def test_build_short_post_text_without_url_falls_back_to_plain_source() -> None:
    text = build_short_post_text(
        source_name="BBC & Sport",
        source_url="",
        source_tier="tier1",
        stars=4,
        news="Оттавио близок к переходу в ПСЖ после согласования суммы и личных условий.",
    )
    assert "📡 BBC &amp; Sport" in text
    assert "<a href=" not in text


def test_validate_comment_accepts_single_human_insert() -> None:
    comment = (
        "Наконец-то клуб добирает игрока именно под слабую позицию. "
        "По логике состава этот трансфер давно напрашивался."
    )
    problems = validate_comment_text(
        comment,
        insert_used="наконец-то",
        news="Оттавио близок к переходу в ПСЖ после согласования суммы.",
        stars=4,
        recent_inserts=["к сожалению"],
    )
    assert problems == []


def test_validate_comment_rejects_recent_insert_reuse() -> None:
    comment = (
        "Наконец-то клуб закрывает дыру в составе без лишнего шума. "
        "Для этой позиции ход выглядит логичным."
    )
    problems = validate_comment_text(
        comment,
        insert_used="наконец-то",
        news="Оттавио близок к переходу в ПСЖ после согласования суммы.",
        stars=4,
        recent_inserts=["наконец-то"],
    )
    assert "insert_used уже был в recent_inserts" in problems


def test_validate_comment_rejects_skepticism_with_five_stars() -> None:
    comment = (
        "Верится с трудом, хотя сделка уже подтверждена клубом. "
        "Сам выбор всё равно выглядит спорно."
    )
    problems = validate_comment_text(
        comment,
        insert_used="верится с трудом",
        news="Клуб официально объявил о переходе игрока.",
        stars=5,
        recent_inserts=[],
    )
    assert "скепсис запрещён при 5 звёздах" in problems


def test_apply_insert_to_comment_replaces_old_insert() -> None:
    comment = "К сожалению, клуб снова берёт игрока не под главную проблему состава."
    repaired = apply_insert_to_comment(comment, "неожиданно")
    assert repaired.startswith("Неожиданно ")
    assert "К сожалению" not in repaired
