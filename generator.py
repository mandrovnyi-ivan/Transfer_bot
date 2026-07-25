from __future__ import annotations

import asyncio
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
аргумент на логике (позиция, возраст, конкуренция в составе, глубина скамейки).

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
class GeneratedPost:
    news: str
    reliability_note: str
    comment: str
    insert_used: str


@dataclass(slots=True)
class GenerationResult:
    payload: GeneratedPost
    text: str
    problems: list[str]
    warnings: list[str]


def render_stars(stars: int) -> str:
    stars = max(1, min(5, stars))
    return "★" * stars + "☆" * (5 - stars)


def render_tier_emoji(source_tier: str) -> str:
    return TIER_EMOJI.get(source_tier, TIER_EMOJI["rumor"])


def build_post_text(
    *,
    source_name: str,
    source_url: str,
    source_tier: str,
    stars: int,
    payload: GeneratedPost,
) -> str:
    return (
        f"{render_tier_emoji(source_tier)} {payload.news}\n\n"
        f"📡 {source_name}\n"
        f"Надёжность: {render_stars(stars)}\n"
        f"{payload.reliability_note}\n\n"
        f"{payload.comment}"
    )


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


def repair_generated_post(payload: GeneratedPost, *, stars: int, recent_inserts: list[str]) -> GeneratedPost:
    normalized_insert = normalize_insert_value(payload.insert_used)
    cleaned_news = strip_known_inserts(payload.news)
    detected = extract_used_inserts(f"{cleaned_news}\n{payload.comment}")
    chosen_insert = choose_insert(
        detected=detected,
        insert_used=normalized_insert,
        stars=stars,
        recent_inserts=recent_inserts,
    )
    cleaned_comment = apply_insert_to_comment(payload.comment, chosen_insert)
    return GeneratedPost(
        news=cleaned_news,
        reliability_note=payload.reliability_note.strip(),
        comment=cleaned_comment,
        insert_used=chosen_insert,
    )


def validate_post_payload(
    payload: GeneratedPost,
    *,
    stars: int,
    recent_inserts: list[str],
    source_name: str,
    source_url: str,
    source_tier: str,
) -> tuple[list[str], str]:
    text = build_post_text(
        source_name=source_name,
        source_url=source_url,
        source_tier=source_tier,
        stars=stars,
        payload=payload,
    )
    problems: list[str] = []

    if not 240 <= len(text) <= 420:
        problems.append("длина вне диапазона 240–420")

    used_inserts = extract_used_inserts(f"{payload.news}\n{payload.comment}")
    if len(used_inserts) != 1:
        problems.append("нужна ровно одна конструкция из списка")
    if payload.insert_used.casefold() not in used_inserts:
        problems.append("insert_used не совпадает с текстом")
    if payload.insert_used.casefold() in {item.casefold() for item in recent_inserts}:
        problems.append("insert_used уже был в recent_inserts")
    if stars == 5 and payload.insert_used.casefold() in SKEPTICISM_INSERTS:
        problems.append("скепсис запрещён при 5 звёздах")

    joined_lower = f"{payload.news}\n{payload.reliability_note}\n{payload.comment}".casefold()
    for phrase in BANNED_PHRASES:
        if phrase in joined_lower:
            problems.append(f"запрещённая фраза: {phrase}")
            break

    if "посмотрим" in joined_lower:
        problems.append("есть запрещённое слово «посмотрим»")

    if EMOJI_RE.search(payload.news) or EMOJI_RE.search(payload.comment):
        problems.append("эмодзи запрещены в news/comment")
    if "!" in payload.news or "!" in payload.comment:
        problems.append("восклицательные знаки запрещены в news/comment")

    if fuzz.ratio(payload.news, payload.comment) >= 70:
        problems.append("comment дублирует news")
    if "надёжность:" in payload.reliability_note.casefold():
        problems.append("reliability_note дублирует служебную строку")

    return problems, text


def parse_generation_json(text: str) -> GeneratedPost:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    payload = json.loads(cleaned)
    return GeneratedPost(
        news=str(payload["news"]).strip(),
        reliability_note=str(payload["reliability_note"]).strip(),
        comment=str(payload["comment"]).strip(),
        insert_used=str(payload["insert_used"]).strip(),
    )


class PostGenerator:
    def __init__(self, api_key: str, model: str) -> None:
        if AsyncAnthropic is None:
            raise RuntimeError("anthropic package is required to generate posts")
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def generate(
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
        recent_inserts: list[str],
    ) -> GenerationResult:
        problems_hint: list[str] = []
        warnings: list[str] = []
        last_payload: GeneratedPost | None = None
        last_text = ""

        for attempt in range(2):
            payload = await self._call_model(
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
                recent_inserts=recent_inserts,
                failed_checks=problems_hint,
            )
            payload = repair_generated_post(payload, stars=stars, recent_inserts=recent_inserts)
            problems, text = validate_post_payload(
                payload,
                stars=stars,
                recent_inserts=recent_inserts,
                source_name=source_name,
                source_url=source_url,
                source_tier=source_tier,
            )
            last_payload = payload
            last_text = text
            if not problems:
                return GenerationResult(payload=payload, text=text, problems=[], warnings=[])
            problems_hint = problems

        warnings = [f"⚠️ {', '.join(problems_hint)}"] if problems_hint else []
        return GenerationResult(
            payload=last_payload or GeneratedPost(news="", reliability_note="", comment="", insert_used=""),
            text=last_text,
            problems=problems_hint,
            warnings=warnings,
        )

    async def _call_model(
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
        recent_inserts: list[str],
        failed_checks: list[str],
    ) -> GeneratedPost:
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
- recent_inserts: {", ".join(recent_inserts) if recent_inserts else "нет"}

ПРАВИЛА:
- Пост 240–420 знаков вместе с итоговой сборкой.
- Четыре блока: новость → источник → звёзды → комментарий.
- Пиши очень компактно: news 1–2 коротких предложения, reliability_note 1 короткое предложение, comment 2 коротких предложения.
- В блоке comment нужна радикальная однозначная позиция без «посмотрим» и «время покажет».
- В comment ОБЯЗАТЕЛЬНО используй ровно одну конструкцию из списка, а поле insert_used верни точной строкой этой конструкции.
- В reliability_note не пиши служебные строки вроде «Надёжность: ★★★★★» и не повторяй оформление итогового шаблона.
- Используй только факты из RAW_CONTEXT.
- Верни строго JSON:
  {{"news":"...","reliability_note":"...","comment":"...","insert_used":"..."}}

Эталонные примеры:
{EXAMPLES}
""".strip()
        if failed_checks:
            user_prompt += "\n\nИсправь ошибки предыдущей версии: " + "; ".join(failed_checks)

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
                return parse_generation_json(text)
            except Exception as exc:
                last_error = exc
                if attempt == 2:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError(f"post generation failed: {last_error}")
