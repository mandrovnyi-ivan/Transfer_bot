from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

try:
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - exercised only without runtime deps
    AsyncAnthropic = None  # type: ignore[assignment]

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - exercised only without runtime deps
    class _FallbackFuzz:
        @staticmethod
        def ratio(left: str, right: str) -> float:
            return SequenceMatcher(None, left, right).ratio() * 100

    fuzz = _FallbackFuzz()


SYSTEM_PROMPT = """
Ты пишешь короткие новостные посты для Telegram-канала бренда Artificial Analyst.
Позиционирование: холодная аналитика в горячей упаковке. AI-бренд, не эксперт-человек.

Ты работаешь ТОЛЬКО с фактами из RAW_CONTEXT. Запрещено добавлять статистику,
которой нет в переданном тексте. Не хватает цифры — не выдумывай, строй
аргумент на логике.

ЗАПРЕЩЕНО:
— «Толпа видит… но алгоритм видит…» и любые варианты.
— Пафосные слоганы: «это не эмоции, это математика», «мы не гадаем, мы считаем».
— «Занос дня», «проходняк», «инсайд», «100% ставка».
— Гарантии результата в любой форме.
— «Я потратил часы на анализ» — работу делает алгоритм.
— Эмодзи в тексте.
— Восклицательные знаки, «ого», «вау», «жесть», «капец».
— Обращение «ты/вы».
— Короткая рубленая фраза без конкретного факта.

ЛЕКСИКА: «проигрыш» → «минус», «провал» → «промах».
""".strip()

EXAMPLES = """
★★★★★ / official:
«Наконец-то Юнайтед покупает под схему, а не под имя. Вопрос в другом:
это четвёртый опорник за три окна, и предыдущие три всё ещё в составе.»

★★★☆☆ / talks:
«К сожалению для Милана, это ровно тот профиль игрока, которого они уже
продали прошлым летом. Круг замкнулся за одиннадцать месяцев.»

★★☆☆☆ / rumor:
«Верится с трудом. Испанская пресса переписывает эту историю каждое окно,
а игрок с 2023 года не менял агента и не давал ни одного намёка.»

★☆☆☆☆ / interest:
«Ну да, конечно. Интерес есть у всех и всегда — это не новость,
это заполнение эфира.»

★★★★☆ / medical:
«Обидно, но для Байера это лучший из возможных сценариев. Продать за 130
того, кого через год пришлось бы отпускать за 60.»
""".strip()

INSERT_GROUPS: dict[str, tuple[str, ...]] = {
    "regret": ("к сожалению", "как ни печально", "обидно, но", "жаль, что"),
    "approval": ("слава богу", "наконец-то", "хорошо, что", "и вовремя"),
    "skepticism": (
        "верится с трудом",
        "честно говоря, сомнительно",
        "ну да, конечно",
        "посмотрим, кто это подтвердит",
        "пока не верим",
    ),
    "obviousness": ("что и требовалось доказать", "этого ждали все", "тут без сюрпризов"),
    "surprise": ("неожиданно", "вот этого никто не ждал", "странное решение"),
    "fatigue": ("опять", "снова", "в третий раз за окно", "этот сериал продолжается"),
}
ALL_INSERTS = tuple(insert for values in INSERT_GROUPS.values() for insert in values)
SKEPTICISM_INSERTS = set(INSERT_GROUPS["skepticism"])
BANNED_PHRASES = (
    "толпа видит",
    "алгоритм видит",
    "это не эмоции, это математика",
    "мы не гадаем, мы считаем",
    "занос дня",
    "проходняк",
    "инсайд",
    "100% ставка",
    "я потратил часы на анализ",
    "время покажет",
)
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]",
    flags=re.UNICODE,
)
TIER_EMOJI = {
    "official": "🟢",
    "tier1": "🔵",
    "tier2": "🟡",
    "yellow": "🟠",
    "rumor": "⚪",
}


@dataclass(slots=True)
class ShortPostResult:
    news: str
    text: str
    problems: list[str]


@dataclass(slots=True)
class CommentResult:
    comment: str
    insert_used: str
    text: str
    problems: list[str]


def render_stars(stars: int) -> str:
    stars = max(1, min(5, stars))
    return "★" * stars + "☆" * (5 - stars)


def render_tier_emoji(source_tier: str) -> str:
    return TIER_EMOJI.get(source_tier, TIER_EMOJI["rumor"])


def build_short_post_text(
    *,
    source_name: str,
    source_url: str,
    source_tier: str,
    stars: int,
    news: str,
    comment: str | None = None,
) -> str:
    escaped_source_name = html.escape(source_name or "Источник неизвестен", quote=False)
    escaped_source_url = html.escape(source_url or "", quote=True)
    source_line = (
        f'📡 <a href="{escaped_source_url}">{escaped_source_name}</a>'
        if escaped_source_url
        else f"📡 {escaped_source_name}"
    )
    parts = [
        f"{render_tier_emoji(source_tier)} {news.strip()}",
        "",
        source_line,
        f"Надёжность: {render_stars(stars)}",
    ]
    if comment:
        parts.extend(["", comment.strip()])
    return "\n".join(parts)


def extract_used_inserts(text: str) -> list[str]:
    lowered = text.casefold()
    found = [insert for insert in ALL_INSERTS if insert in lowered]
    return sorted(set(found), key=found.index)


def normalize_insert_value(value: str | None) -> str:
    normalized = str(value or "").strip().casefold()
    return "" if normalized in {"", "none", "null", "нет"} else normalized


def strip_known_inserts(text: str) -> str:
    cleaned = text.strip()
    for insert in sorted(ALL_INSERTS, key=len, reverse=True):
        pattern = re.compile(rf"(?i)(?<!\w){re.escape(insert)}(?!\w)[,\s]*")
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"^[,\s.:-]+", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def choose_insert(*, detected: list[str], insert_used: str, stars: int, recent_inserts: list[str]) -> str:
    recent = {normalize_insert_value(item) for item in recent_inserts}
    candidates = [
        insert
        for insert in ALL_INSERTS
        if insert not in recent and not (stars == 5 and insert in SKEPTICISM_INSERTS)
    ]
    if len(detected) == 1 and detected[0] in candidates:
        return detected[0]
    if insert_used and insert_used in candidates:
        return insert_used
    return candidates[0] if candidates else (detected[0] if detected else insert_used)


def apply_insert_to_comment(comment: str, insert: str) -> str:
    base = strip_known_inserts(comment)
    if not insert:
        return base
    if not base:
        return insert.capitalize()
    head = base[0].lower() + base[1:] if len(base) > 1 else base.lower()
    return f"{insert.capitalize()} {head}"


def validate_news_text(news: str) -> list[str]:
    problems: list[str] = []
    cleaned = news.strip()
    if not 80 <= len(cleaned) <= 600:
        problems.append("длина news вне диапазона 80–600")
    if EMOJI_RE.search(cleaned):
        problems.append("эмодзи запрещены в news")
    if "!" in cleaned:
        problems.append("восклицательные знаки запрещены в news")
    lowered = cleaned.casefold()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f"запрещённая фраза: {phrase}")
            break
    return problems


def validate_comment_text(
    comment: str,
    *,
    insert_used: str,
    news: str,
    stars: int,
    recent_inserts: list[str],
) -> list[str]:
    problems: list[str] = []
    cleaned = comment.strip()
    used_inserts = extract_used_inserts(cleaned)
    normalized_insert = normalize_insert_value(insert_used)

    if len(used_inserts) != 1:
        problems.append("нужна ровно одна конструкция из списка")
    if normalized_insert and normalized_insert not in used_inserts:
        problems.append("insert_used не совпадает с текстом")
    recent = {normalize_insert_value(item) for item in recent_inserts}
    if normalized_insert and normalized_insert in recent:
        problems.append("insert_used уже был в recent_inserts")
    if normalized_insert in SKEPTICISM_INSERTS and stars == 5:
        problems.append("скепсис запрещён при 5 звёздах")

    lowered = cleaned.casefold()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f"запрещённая фраза: {phrase}")
            break
    if "посмотрим" in lowered:
        problems.append("есть запрещённое слово «посмотрим»")
    if EMOJI_RE.search(cleaned):
        problems.append("эмодзи запрещены в comment")
    if "!" in cleaned:
        problems.append("восклицательные знаки запрещены в comment")
    if fuzz.ratio(news, cleaned) >= 70:
        problems.append("comment дублирует news")
    return problems


def parse_json_object(text: str) -> dict[str, str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    payload = json.loads(cleaned)
    return {str(key): str(value).strip() for key, value in payload.items()}


class PostGenerator:
    def __init__(self, api_key: str, model: str) -> None:
        if AsyncAnthropic is None:
            raise RuntimeError("anthropic package is required to generate posts")
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def generate_news(
        self,
        *,
        source_name: str,
        source_url: str,
        source_tier: str,
        title: str,
        raw_text: str,
        player_name: str,
        from_club: str | None,
        to_club: str | None,
        fee: str | None,
        news_type: str,
        stars: int,
    ) -> ShortPostResult:
        problems_hint: list[str] = []
        best_news = ""
        best_text = ""
        best_problems: list[str] | None = None

        for _ in range(2):
            news = await self._call_news_model(
                source_name=source_name,
                source_url=source_url,
                source_tier=source_tier,
                title=title,
                raw_text=raw_text,
                player_name=player_name,
                from_club=from_club,
                to_club=to_club,
                fee=fee,
                news_type=news_type,
                stars=stars,
                failed_checks=problems_hint,
            )
            problems = validate_news_text(news)
            text = build_short_post_text(
                source_name=source_name,
                source_url=source_url,
                source_tier=source_tier,
                stars=stars,
                news=news,
            )
            if best_problems is None or len(problems) < len(best_problems):
                best_news = news
                best_text = text
                best_problems = list(problems)
            if not problems:
                return ShortPostResult(news=news, text=text, problems=[])
            problems_hint = problems

        return ShortPostResult(news=best_news, text=best_text, problems=best_problems or problems_hint)

    async def generate_comment(
        self,
        *,
        source_name: str,
        source_url: str,
        source_tier: str,
        title: str,
        raw_text: str,
        player_name: str,
        from_club: str | None,
        to_club: str | None,
        fee: str | None,
        news_type: str,
        stars: int,
        reasons: list[str],
        news: str,
        recent_inserts: list[str],
    ) -> CommentResult:
        problems_hint: list[str] = []
        best_comment = ""
        best_insert = ""
        best_text = ""
        best_problems: list[str] | None = None

        for _ in range(3):
            comment, insert_used = await self._call_comment_model(
                source_name=source_name,
                source_url=source_url,
                source_tier=source_tier,
                title=title,
                raw_text=raw_text,
                player_name=player_name,
                from_club=from_club,
                to_club=to_club,
                fee=fee,
                news_type=news_type,
                stars=stars,
                reasons=reasons,
                news=news,
                recent_inserts=recent_inserts,
                failed_checks=problems_hint,
            )
            detected = extract_used_inserts(comment)
            chosen_insert = choose_insert(
                detected=detected,
                insert_used=normalize_insert_value(insert_used),
                stars=stars,
                recent_inserts=recent_inserts,
            )
            repaired_comment = apply_insert_to_comment(comment, chosen_insert)
            problems = validate_comment_text(
                repaired_comment,
                insert_used=chosen_insert,
                news=news,
                stars=stars,
                recent_inserts=recent_inserts,
            )
            text = build_short_post_text(
                source_name=source_name,
                source_url=source_url,
                source_tier=source_tier,
                stars=stars,
                news=news,
                comment=repaired_comment,
            )
            if best_problems is None or len(problems) < len(best_problems):
                best_comment = repaired_comment
                best_insert = chosen_insert
                best_text = text
                best_problems = list(problems)
            if not problems:
                return CommentResult(
                    comment=repaired_comment,
                    insert_used=chosen_insert,
                    text=text,
                    problems=[],
                )
            problems_hint = problems

        return CommentResult(
            comment=best_comment,
            insert_used=best_insert,
            text=best_text,
            problems=best_problems or problems_hint,
        )

    async def _call_news_model(
        self,
        *,
        source_name: str,
        source_url: str,
        source_tier: str,
        title: str,
        raw_text: str,
        player_name: str,
        from_club: str | None,
        to_club: str | None,
        fee: str | None,
        news_type: str,
        stars: int,
        failed_checks: list[str],
    ) -> str:
        user_prompt = f"""
RAW_CONTEXT:
- source_name: {source_name}
- source_tier: {source_tier}
- source_url: {source_url}
- title: {title}
- raw_text: {raw_text}
- player_name: {player_name}
- from_club: {from_club or "не указано"}
- to_club: {to_club or "не указано"}
- fee: {fee or "не указана"}
- type: {news_type}
- stars: {render_stars(stars)}

ЗАДАЧА:
- Верни только JSON: {{"news":"..."}}
- Сгенерируй только блок news.
- News должен быть 1–3 предложения, сухо, без оценки, только факты из RAW_CONTEXT.
- Не добавляй комментарий, обоснование надёжности, ссылку или оформление.
""".strip()
        if failed_checks:
            user_prompt += "\n\nИсправь ошибки предыдущей версии: " + "; ".join(failed_checks)
        payload = await self._call_model_json(user_prompt)
        return payload.get("news", "").strip()

    async def _call_comment_model(
        self,
        *,
        source_name: str,
        source_url: str,
        source_tier: str,
        title: str,
        raw_text: str,
        player_name: str,
        from_club: str | None,
        to_club: str | None,
        fee: str | None,
        news_type: str,
        stars: int,
        reasons: list[str],
        news: str,
        recent_inserts: list[str],
        failed_checks: list[str],
    ) -> tuple[str, str]:
        user_prompt = f"""
RAW_CONTEXT:
- source_name: {source_name}
- source_tier: {source_tier}
- source_url: {source_url}
- title: {title}
- raw_text: {raw_text}
- player_name: {player_name}
- from_club: {from_club or "не указано"}
- to_club: {to_club or "не указано"}
- fee: {fee or "не указана"}
- type: {news_type}
- stars: {render_stars(stars)}
- reasons: {", ".join(reasons)}
- news: {news}
- recent_inserts: {", ".join(recent_inserts) if recent_inserts else "нет"}

ЗАДАЧА:
- Верни только JSON: {{"comment":"...","insert_used":"..."}}
- Comment должен быть 2–3 предложения в tone of voice бренда.
- Нужна радикальная позиция без «посмотрим» и «время покажет».
- Используй ровно одну человечную конструкцию из набора и верни её точной строкой в insert_used.
- Не повторяй news и не выдумывай факты вне RAW_CONTEXT.

Эталонные примеры:
{EXAMPLES}
""".strip()
        if failed_checks:
            user_prompt += "\n\nИсправь ошибки предыдущей версии: " + "; ".join(failed_checks)
        payload = await self._call_model_json(user_prompt)
        return payload.get("comment", "").strip(), payload.get("insert_used", "").strip()

    async def _call_model_json(self, user_prompt: str) -> dict[str, str]:
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=700,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
                return parse_json_object(text)
            except Exception as exc:
                last_error = exc
                if attempt == 2:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError(f"post generation failed: {last_error}")
