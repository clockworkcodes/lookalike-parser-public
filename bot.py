"""
Lookalike / Chaining парсер Instagram: Telegram-бот (aiogram 3, long polling)
+ веб-сервер aiohttp (Mini App, API, health-check) в одном event loop.

Переменные окружения:
    BOT_TOKEN            — токен бота
    ALLOWED_USER_ID      — Telegram ID владельца
    IG_SESSION_JSON      — JSON-дамп сессии instagrapi (см. generate_session.py)
    IG_PROXY             — опционально, http://user:pass@ip:port
    RENDER_EXTERNAL_URL  — публичный URL сервиса (Render подставляет сам)
    PORT                 — порт веб-сервера (Render подставляет сам, по умолчанию 10000)
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    FeedbackRequired,
    InvalidTargetUser,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    UserNotFound,
)
from instagrapi.types import UserShort

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("lookalike")
logging.getLogger("instagrapi").setLevel(logging.WARNING)
logging.getLogger("public_request").setLevel(logging.WARNING)
logging.getLogger("private_request").setLevel(logging.WARNING)

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} должен быть целым числом, получено: {raw!r}") from exc


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = _env_int("ALLOWED_USER_ID", 0)
IG_SESSION_JSON = os.getenv("IG_SESSION_JSON", "").strip()
IG_PROXY = os.getenv("IG_PROXY", "").strip() or None
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
PORT = _env_int("PORT", 10000)

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"
SESSION_PATH = BASE_DIR / "session.json"
WEBAPP_URL = f"{RENDER_EXTERNAL_URL}/" if RENDER_EXTERNAL_URL else ""

MIN_ACCOUNTS = 10
MAX_ACCOUNTS = 200
DEFAULT_ACCOUNTS = 50
MAX_SEEDS = 10
MAX_KEYWORDS = 30

# time.sleep(random.uniform(5, 15)) перед КАЖДЫМ HTTP-запросом instagrapi (через Client.delay_range).
# Выполняется в рабочем потоке asyncio.to_thread, поэтому event loop бота не блокируется.
REQUEST_DELAY_MIN, REQUEST_DELAY_MAX = 5.0, 15.0
BACKOFF_MIN, BACKOFF_MAX = 60.0, 120.0
SESSION_SAVE_EVERY = 20
STATUS_INTERVAL = 5.0
MAX_CONSECUTIVE_ERRORS = 6
MAX_QUEUE = 3000
CAPTION_PREVIEW = 15
INIT_DATA_MAX_AGE = timedelta(hours=24)

STOP_CALLBACK = "job:stop"
USERNAME_RE = re.compile(r"^[a-z0-9._]{1,30}$")
IG_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]+)", re.IGNORECASE)

CSV_FIELDS = [
    "username",
    "full_name",
    "profile_url",
    "pk",
    "is_private",
    "is_verified",
    "followers",
    "following",
    "media_count",
    "category",
    "biography",
    "external_url",
    "source",
    "depth",
]

# --------------------------------------------------------------------------- #
# Instagram-клиент
# --------------------------------------------------------------------------- #

ig_client: Client | None = None
ig_init_error: str | None = None


def session_id_of(settings: dict[str, Any]) -> str | None:
    auth = settings.get("authorization_data") or {}
    return auth.get("sessionid") or (settings.get("cookies") or {}).get("sessionid")


def read_saved_session() -> dict[str, Any] | None:
    if not SESSION_PATH.exists():
        return None
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Instagram: сохранённый session.json повреждён (%s), беру IG_SESSION_JSON", exc)
        return None
    return data if isinstance(data, dict) else None


def save_session(cl: Client) -> None:
    """
    Атомарно сохраняет текущие настройки сессии (cookies, uuids, device) через dump_settings.
    Синхронная функция: вызывать через asyncio.to_thread.
    """
    tmp_path = SESSION_PATH.with_suffix(".json.tmp")
    try:
        cl.dump_settings(tmp_path)
        os.replace(tmp_path, SESSION_PATH)
    except Exception as exc:
        logger.warning("Instagram: не удалось сохранить сессию: %s", exc)


def init_instagram() -> None:
    """
    Загружает сессию instagrapi без повторного логина.

    Приоритет: session.json, сохранённый ботом ранее (в нём свежие cookies),
    если это та же сессия, что в IG_SESSION_JSON. Если в переменной окружения
    другая сессия (её перегенерировали) — берётся она и перезаписывает файл.
    """
    global ig_client, ig_init_error

    if not IG_SESSION_JSON:
        ig_init_error = "переменная IG_SESSION_JSON не задана"
        logger.error("Instagram: %s", ig_init_error)
        return

    try:
        env_settings = json.loads(IG_SESSION_JSON)
    except json.JSONDecodeError as exc:
        ig_init_error = f"IG_SESSION_JSON — невалидный JSON ({exc})"
        logger.error("Instagram: %s", ig_init_error)
        return
    if not isinstance(env_settings, dict) or not session_id_of(env_settings):
        ig_init_error = "в IG_SESSION_JSON нет sessionid — сгенерируйте сессию через generate_session.py"
        logger.error("Instagram: %s", ig_init_error)
        return

    saved = read_saved_session()
    if saved and session_id_of(saved) == session_id_of(env_settings):
        source = "session.json (сохранённая ботом)"
    else:
        SESSION_PATH.write_text(json.dumps(env_settings), encoding="utf-8")
        source = "IG_SESSION_JSON"

    try:
        cl = Client()
        cl.load_settings(str(SESSION_PATH))
        cl.delay_range = [REQUEST_DELAY_MIN, REQUEST_DELAY_MAX]
        if IG_PROXY:
            cl.set_proxy(IG_PROXY)
        ig_client = cl
        ig_init_error = None
        logger.info("Instagram: сессия загружена из %s%s", source, " (через прокси)" if IG_PROXY else "")
    except Exception as exc:
        ig_init_error = f"не удалось загрузить сессию: {exc}"
        logger.exception("Instagram: %s", ig_init_error)


# --------------------------------------------------------------------------- #
# Модели задачи и глобальное состояние
# --------------------------------------------------------------------------- #


class FatalJobError(Exception):
    """Ошибка, после которой продолжать парсинг бессмысленно."""


@dataclass
class JobParams:
    seeds: list[str]
    max_accounts: int
    keywords: list[str]
    deep_check: bool
    min_followers: int | None = None
    max_followers: int | None = None


@dataclass
class JobState:
    is_running: bool = False
    cancel_requested: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    params: JobParams | None = None
    chat_id: int | None = None
    status_message_id: int | None = None
    task: asyncio.Task | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    checked: int = 0
    requests: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    queue_size: int = 0
    current: str = ""
    started_at: float = 0.0
    next_status_at: float = 0.0
    finish_reason: str = ""
    error_text: str = ""
    related_ok: int = 0
    related_empty: int = 0
    following_fallbacks: int = 0

    def reset(self, params: JobParams, chat_id: int) -> None:
        self.is_running = True
        self.cancel_requested = False
        self.cancel_event = asyncio.Event()
        self.params = params
        self.chat_id = chat_id
        self.status_message_id = None
        self.task = None
        self.results = []
        self.checked = 0
        self.requests = 0
        self.errors = 0
        self.consecutive_errors = 0
        self.queue_size = 0
        self.current = "подготовка"
        self.started_at = time.monotonic()
        self.next_status_at = 0.0
        self.finish_reason = ""
        self.error_text = ""
        self.related_ok = 0
        self.related_empty = 0
        self.following_fallbacks = 0

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.cancel_event.set()

    @property
    def is_full(self) -> bool:
        return self.params is not None and len(self.results) >= self.params.max_accounts

    @property
    def should_stop(self) -> bool:
        return self.cancel_requested or self.is_full


STATE = JobState()

# --------------------------------------------------------------------------- #
# Парсинг входных параметров
# --------------------------------------------------------------------------- #


def normalize_username(token: str) -> str | None:
    token = token.strip()
    if not token:
        return None
    match = IG_URL_RE.search(token)
    if match:
        token = match.group(1)
    token = token.lstrip("@").strip().lower()
    return token if USERNAME_RE.match(token) else None


def parse_seeds(raw: str) -> list[str]:
    seeds: list[str] = []
    for token in re.split(r"[\s,;]+", raw or ""):
        username = normalize_username(token)
        if username and username not in seeds:
            seeds.append(username)
    return seeds[:MAX_SEEDS]


def parse_keywords(raw: str) -> list[str]:
    keywords: list[str] = []
    for token in re.split(r"[,;\n]+", raw or ""):
        keyword = token.strip().lower()
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords[:MAX_KEYWORDS]


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "да", "deep"}


def parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    text = re.sub(r"[\s_]", "", str(value))
    if not text:
        return None
    try:
        number = int(float(text))
    except ValueError as exc:
        raise ValueError(f"Некорректное число подписчиков: {value}") from exc
    if number < 0:
        raise ValueError("Число подписчиков не может быть отрицательным")
    return number


def build_params(
    seed_accounts: Any,
    max_accounts: Any,
    keyword_filter: Any,
    deep_check: Any,
    min_followers: Any = None,
    max_followers: Any = None,
) -> JobParams:
    seeds = parse_seeds(str(seed_accounts or ""))
    if not seeds:
        raise ValueError("Укажите хотя бы один корректный seed-аккаунт")

    try:
        limit = int(max_accounts)
    except (TypeError, ValueError):
        limit = DEFAULT_ACCOUNTS
    limit = max(MIN_ACCOUNTS, min(MAX_ACCOUNTS, limit))

    deep = parse_bool(deep_check)
    min_f = parse_optional_int(min_followers) if deep else None
    max_f = parse_optional_int(max_followers) if deep else None
    if min_f is not None and max_f is not None and min_f > max_f:
        raise ValueError("Минимум подписчиков больше максимума")

    return JobParams(
        seeds=seeds,
        max_accounts=limit,
        keywords=parse_keywords(str(keyword_filter or "")),
        deep_check=deep,
        min_followers=min_f,
        max_followers=max_f,
    )


def parse_command_args(args: str) -> JobParams:
    """
    Форматы:
        /parse username1, username2
        /parse seeds=user1,user2 limit=80 kw=prod,beats deep=1 min=1000 max=50000
    """
    args = (args or "").strip()
    if "=" not in args:
        return build_params(args, DEFAULT_ACCOUNTS, "", False)

    pairs = re.findall(r"(\w+)\s*=\s*(.*?)(?=\s+\w+\s*=|$)", args, flags=re.DOTALL)
    options = {key.lower(): value.strip() for key, value in pairs}
    return build_params(
        seed_accounts=options.get("seeds") or options.get("seed", ""),
        max_accounts=options.get("limit") or options.get("max_accounts", DEFAULT_ACCOUNTS),
        keyword_filter=options.get("kw") or options.get("keywords", ""),
        deep_check=options.get("deep", "0"),
        min_followers=options.get("min"),
        max_followers=options.get("max"),
    )


def matches_keywords(keywords: list[str], *fields: str | None) -> bool:
    if not keywords:
        return True
    haystack = " ".join(f for f in fields if f).lower()
    return any(keyword in haystack for keyword in keywords)


# --------------------------------------------------------------------------- #
# Telegram: статус-сообщение
# --------------------------------------------------------------------------- #


def stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛑 Остановить и выгрузить найденное", callback_data=STOP_CALLBACK)]
        ]
    )


def webapp_keyboard() -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚀 Открыть парсер", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


def ig_link(username: str) -> str:
    """Кликабельная ссылка на профиль Instagram (иначе Telegram сам сделает @ник ссылкой на Telegram)."""
    username = html.escape(username or "")
    return f'<a href="https://instagram.com/{username}">@{username}</a>'


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def progress_bar(done: int, total: int, cells: int = 12) -> str:
    ratio = 0.0 if total <= 0 else min(1.0, done / total)
    filled = round(ratio * cells)
    return "▰" * filled + "▱" * (cells - filled)


FINISH_TITLES = {
    "limit": "✅ Готово — лимит достигнут",
    "cancel": "🛑 Остановлено пользователем",
    "exhausted": "✅ Готово — граф рекомендаций исчерпан",
    "error": "⚠️ Остановлено из-за ошибки",
}


def render_status(final: bool = False) -> str:
    p = STATE.params
    if p is None:
        return "Нет активной задачи."

    if final:
        title = FINISH_TITLES.get(STATE.finish_reason, "✅ Готово")
    elif STATE.cancel_requested:
        title = "⏳ Останавливаю и собираю файл…"
    else:
        title = "🔎 Парсинг идёт…"

    found = len(STATE.results)
    mode = "🔬 Глубокий (био, ссылки, подписчики)" if p.deep_check else "⚡ Быстрый (username + имя)"
    keywords = ", ".join(html.escape(k) for k in p.keywords) if p.keywords else "—"
    seeds = ", ".join(ig_link(s) for s in p.seeds)

    lines = [
        f"<b>{title}</b>",
        "",
        f"🌱 Seed: {seeds}",
        f"🧭 Режим: {mode}",
        f"🏷 Ключевые слова: {keywords}",
    ]
    if p.deep_check and (p.min_followers is not None or p.max_followers is not None):
        lo = f"{p.min_followers:,}".replace(",", " ") if p.min_followers is not None else "0"
        hi = f"{p.max_followers:,}".replace(",", " ") if p.max_followers is not None else "∞"
        lines.append(f"👥 Подписчики: {lo} – {hi}")

    lines += [
        "",
        f"{progress_bar(found, p.max_accounts)}  <b>{found}</b> / {p.max_accounts}",
        f"👀 Проверено профилей: {STATE.checked}",
        f"🗂 В очереди графа: {STATE.queue_size}",
        f"📡 Запросов к IG: {STATE.requests}  ·  ⚠️ Ошибок: {STATE.errors}",
        f"⏱ {format_duration(time.monotonic() - STATE.started_at)}",
    ]
    if not final and STATE.current:
        current = re.sub(r"@([A-Za-z0-9._]+)", lambda m: ig_link(m.group(1)), html.escape(STATE.current))
        lines.append(f"📍 Сейчас: {current}")
    if final and STATE.error_text:
        lines += ["", f"<i>{html.escape(STATE.error_text[:300])}</i>"]
    return "\n".join(lines)


async def update_status(bot: Bot, force: bool = False, final: bool = False) -> None:
    """Обновляет статус-сообщение не чаще раза в STATUS_INTERVAL секунд."""
    if STATE.chat_id is None or STATE.status_message_id is None:
        return

    now = time.monotonic()
    if not force and not final and now < STATE.next_status_at:
        return

    attempts = 3 if final else 1
    for _ in range(attempts):
        try:
            await bot.edit_message_text(
                text=render_status(final=final),
                chat_id=STATE.chat_id,
                message_id=STATE.status_message_id,
                reply_markup=None if final else stop_keyboard(),
            )
            STATE.next_status_at = time.monotonic() + STATUS_INTERVAL
            return
        except TelegramRetryAfter as exc:
            STATE.next_status_at = time.monotonic() + exc.retry_after + 1
            logger.warning("Telegram flood control: retry after %s s", exc.retry_after)
            if final:
                await asyncio.sleep(exc.retry_after + 1)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                STATE.next_status_at = time.monotonic() + STATUS_INTERVAL
            else:
                logger.warning("Не удалось обновить статус: %s", exc)
            return
        except TelegramAPIError as exc:
            logger.warning("Ошибка Telegram при обновлении статуса: %s", exc)
            return


async def tg_call(factory: Callable[[], Awaitable[Any]], attempts: int = 3) -> Any:
    """Вызов Telegram API с ожиданием при FloodWait."""
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except TelegramRetryAfter as exc:
            if attempt == attempts:
                raise
            logger.warning("Telegram flood control: жду %s s", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
    return None


# --------------------------------------------------------------------------- #
# Работа с Instagram
# --------------------------------------------------------------------------- #


async def interruptible_sleep(min_s: float = BACKOFF_MIN, max_s: float = BACKOFF_MAX) -> None:
    """Долгая пауза после лимитов Instagram, мгновенно прерывается кнопкой «Остановить»."""
    try:
        await asyncio.wait_for(STATE.cancel_event.wait(), timeout=random.uniform(min_s, max_s))
    except asyncio.TimeoutError:
        pass


def register_error() -> None:
    STATE.errors += 1
    STATE.consecutive_errors += 1
    if STATE.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        raise FatalJobError(
            f"{MAX_CONSECUTIVE_ERRORS} ошибок подряд — вероятно, Instagram ограничил аккаунт или прокси. "
            "Попробуйте позже."
        )


async def ig_call(func: Callable[..., Any], *args: Any, label: str = "") -> Any:
    """
    Выполняет синхронный вызов instagrapi в отдельном потоке.
    Возвращает результат или None при восстановимой ошибке.
    """
    STATE.requests += 1
    try:
        result = await asyncio.to_thread(func, *args)
    except (LoginRequired, ChallengeRequired) as exc:
        raise FatalJobError(
            f"Сессия Instagram недействительна ({type(exc).__name__}). "
            "Сгенерируйте новую через generate_session.py и обновите IG_SESSION_JSON."
        ) from exc
    except UserNotFound:
        logger.info("IG [%s]: пользователь не найден", label)
        return None
    except (PleaseWaitFewMinutes, RateLimitError, FeedbackRequired) as exc:
        logger.warning("IG [%s]: лимит/блокировка %s: %s — пауза", label, type(exc).__name__, exc)
        register_error()
        STATE.current = "пауза из-за лимитов Instagram"
        await interruptible_sleep(BACKOFF_MIN, BACKOFF_MAX)
        return None
    except Exception as exc:
        logger.warning("IG [%s]: %s: %s", label, type(exc).__name__, exc)
        register_error()
        return None

    STATE.consecutive_errors = 0
    if ig_client is not None and STATE.requests % SESSION_SAVE_EVERY == 0:
        await asyncio.to_thread(save_session, ig_client)
    return result


FATAL_OR_LIMIT_ERRORS = (
    LoginRequired,
    ChallengeRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    FeedbackRequired,
)


USER_SHORT_FIELDS = ("username", "full_name", "is_private", "is_verified")


def extract_users_from_payload(payload: Any, exclude_pk: str = "") -> list[UserShort]:
    """
    Достаёт профили из сырого ответа Instagram независимо от имени ключа
    (users / suggestions / items / вложенные node): любой dict с username и числовым pk.
    """
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            pk = node.get("pk") or node.get("pk_id") or node.get("id")
            if node.get("username") and pk is not None and str(pk).isdigit():
                pk = str(pk)
                if pk != exclude_pk and pk not in found:
                    data = {key: node[key] for key in USER_SHORT_FIELDS if node.get(key) is not None}
                    data["pk"] = pk
                    found[pk] = data
                return
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(payload, 0)
    users = []
    for data in found.values():
        try:
            users.append(UserShort(**data))
        except Exception as exc:
            logger.debug("Пропущен профиль %s: %s", data.get("username"), exc)
    return users


def _related_chaining(cl: Client, user_id: str) -> Any:
    """Private API discover/chaining — «Похожие аккаунты» под профилем в приложении."""
    return cl.chaining(user_id)


def _related_fbsearch(cl: Client, user_id: str) -> Any:
    """Private API fbsearch/accounts_recs — «Рекомендуемые» при открытии профиля."""
    return cl.private_request(
        "fbsearch/accounts_recs/",
        params={"target_user_id": user_id, "include_friendship_status": "true"},
    )


def _related_gql(cl: Client, user_id: str) -> Any:
    """Публичный GraphQL edge_chaining (legacy query_hash, Instagram всё чаще его режет)."""
    return cl.user_related_profiles_gql(user_id)


RELATED_SOURCES: dict[str, Callable[[Client, str], Any]] = {
    "chaining": _related_chaining,
    "fbsearch": _related_fbsearch,
    "gql": _related_gql,
}
# Сколько раз источник дал результат — удачный источник пробуется первым (экономит запросы)
related_source_hits: dict[str, int] = {name: 0 for name in RELATED_SOURCES}


def describe_payload(raw: Any) -> str:
    if isinstance(raw, dict):
        return "keys=" + ",".join(list(raw.keys())[:8])
    if isinstance(raw, list):
        return f"list[{len(raw)}]"
    return type(raw).__name__


def run_related_source(cl: Client, name: str, user_id: str) -> tuple[list[Any], str]:
    raw = RELATED_SOURCES[name](cl, user_id)
    if isinstance(raw, list) and all(isinstance(item, UserShort) for item in raw):
        users = [user for user in raw if str(user.pk) != user_id]
    else:
        users = extract_users_from_payload(raw, exclude_pk=user_id)
    return users, describe_payload(raw)


def fetch_related(cl: Client, user_id: str) -> list[Any]:
    """
    Похожие профили из трёх источников по очереди: discover/chaining, fbsearch/accounts_recs,
    GraphQL edge_chaining. Возвращает первый непустой результат.
    Синхронная функция: вызывается через asyncio.to_thread.
    """
    order = sorted(RELATED_SOURCES, key=lambda source: -related_source_hits[source])
    for name in order:
        try:
            users, info = run_related_source(cl, name, user_id)
        except FATAL_OR_LIMIT_ERRORS:
            raise
        except InvalidTargetUser:
            logger.info("Похожие для %s через %s: Instagram отказал (Not eligible for chaining)", user_id, name)
            continue
        except Exception as exc:
            logger.info("Похожие для %s через %s: %s: %s", user_id, name, type(exc).__name__, str(exc)[:200])
            continue
        logger.info("Похожие для %s через %s: %d профилей (%s)", user_id, name, len(users), info)
        if users:
            related_source_hits[name] += 1
            return users
    return []


FOLLOWING_FALLBACK_LIMIT = 50


def fetch_following(cl: Client, user_id: str) -> list[Any]:
    """
    Запасной путь для seed с закрытыми рекомендациями («Not eligible for chaining»):
    его подписки — обычно коллеги, продюсеры и лейбл, у них рекомендации чаще открыты.
    Синхронная функция: вызывается через asyncio.to_thread.
    """
    return cl.user_following_v1(user_id, amount=FOLLOWING_FALLBACK_LIMIT)


def base_row(user: Any, source: str, depth: int) -> dict[str, Any]:
    username = getattr(user, "username", "") or ""
    return {
        "username": username,
        "full_name": getattr(user, "full_name", "") or "",
        "profile_url": f"https://instagram.com/{username}",
        "pk": str(getattr(user, "pk", "") or ""),
        "is_private": bool(getattr(user, "is_private", False)),
        "is_verified": bool(getattr(user, "is_verified", False)),
        "followers": "",
        "following": "",
        "media_count": "",
        "category": "",
        "biography": "",
        "external_url": "",
        "source": source,
        "depth": depth,
    }


async def evaluate_candidate(
    bot: Bot, cl: Client, user: Any, params: JobParams, source: str, depth: int
) -> dict[str, Any] | None:
    row = base_row(user, source, depth)

    if not params.deep_check:
        return row if matches_keywords(params.keywords, row["username"], row["full_name"]) else None

    STATE.current = f"анализ @{row['username']}"
    await update_status(bot)

    info = await ig_call(cl.user_info, row["pk"], label=f"user_info @{row['username']}")
    if info is None:
        return None

    followers = getattr(info, "follower_count", None)
    biography = getattr(info, "biography", "") or ""
    external_url = getattr(info, "external_url", "") or ""
    category = getattr(info, "category_name", None) or getattr(info, "category", None) or ""

    if not matches_keywords(params.keywords, row["username"], row["full_name"], biography, category):
        return None
    if params.min_followers is not None and (followers is None or followers < params.min_followers):
        return None
    if params.max_followers is not None and (followers is None or followers > params.max_followers):
        return None

    row.update(
        {
            "full_name": getattr(info, "full_name", "") or row["full_name"],
            "is_private": bool(getattr(info, "is_private", row["is_private"])),
            "is_verified": bool(getattr(info, "is_verified", row["is_verified"])),
            "followers": followers if followers is not None else "",
            "following": getattr(info, "following_count", "") or "",
            "media_count": getattr(info, "media_count", "") or "",
            "category": category,
            "biography": biography.replace("\r", " ").replace("\n", " ").strip(),
            "external_url": str(external_url),
        }
    )
    return row


async def crawl(bot: Bot, cl: Client, params: JobParams) -> str:
    """BFS по графу рекомендаций. Возвращает причину завершения."""
    queue: deque[tuple[str, str, int]] = deque()
    visited: set[str] = set()

    for username in params.seeds:
        if STATE.cancel_requested:
            return "cancel"
        STATE.current = f"поиск seed @{username}"
        await update_status(bot)
        user_id = await ig_call(cl.user_id_from_username, username, label=f"seed @{username}")
        if user_id:
            pk = str(user_id)
            if pk not in visited:
                visited.add(pk)
                queue.append((pk, username, 0))
        else:
            logger.warning("Seed @%s не найден", username)

    if not queue:
        if STATE.cancel_requested:
            return "cancel"
        raise FatalJobError("Не удалось найти ни одного seed-аккаунта в Instagram")

    while queue and not STATE.should_stop:
        pk, username, depth = queue.popleft()
        STATE.queue_size = len(queue)
        STATE.current = f"рекомендации @{username} (уровень {depth})"
        await update_status(bot)

        related = await ig_call(fetch_related, cl, pk, label=f"related @{username}")
        source = username
        if related:
            STATE.related_ok += 1
        else:
            STATE.related_empty += 1
            if depth == 0 and not STATE.should_stop:
                STATE.current = f"рекомендации @{username} закрыты — беру его подписки"
                await update_status(bot, force=True)
                related = await ig_call(fetch_following, cl, pk, label=f"following @{username}")
                if related:
                    STATE.following_fallbacks += 1
                    source = f"{username} (подписки)"
                    logger.info("Seed @%s: рекомендации закрыты, взято %d подписок", username, len(related))
            if not related:
                continue

        for user in related:
            if STATE.should_stop:
                break
            user_pk = str(getattr(user, "pk", "") or "")
            if not user_pk or user_pk in visited:
                continue
            visited.add(user_pk)

            if len(queue) < MAX_QUEUE:
                queue.append((user_pk, getattr(user, "username", "") or user_pk, depth + 1))
            STATE.queue_size = len(queue)
            STATE.checked += 1

            row = await evaluate_candidate(bot, cl, user, params, source=source, depth=depth + 1)
            if row and not STATE.is_full:
                STATE.results.append(row)
            await update_status(bot)

    if STATE.cancel_requested:
        return "cancel"
    if STATE.is_full:
        return "limit"
    if STATE.related_ok == 0 and STATE.following_fallbacks == 0:
        STATE.error_text = (
            "Instagram не отдал похожие аккаунты ни одним способом (chaining, fbsearch, GraphQL), "
            "а подписки seed недоступны. "
            f"Проверьте источники командой /diag {params.seeds[0]} или попробуйте другой seed."
        )
    elif STATE.related_ok == 0:
        STATE.error_text = (
            "Рекомендации закрыты и у seed, и у его подписок — собраны только подписки seed. "
            "Попробуйте другой seed (например, продюсера или коллегу этого артиста)."
        )
    elif not STATE.results and params.keywords:
        STATE.error_text = (
            f"Просмотрено {STATE.checked} похожих профилей, но ни один не подошёл под ключевые слова. "
            "В быстром режиме слова ищутся только в username и имени — попробуйте глубокий режим "
            "или другие слова."
        )
    return "exhausted"


# --------------------------------------------------------------------------- #
# Выгрузка результатов
# --------------------------------------------------------------------------- #


def csv_safe(value: Any) -> Any:
    """Защита от CSV/формульной инъекции: био вида «=HYPERLINK(...)» не исполнится в Excel."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def build_csv(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: csv_safe(value) for key, value in row.items()})
    return buffer.getvalue().encode("utf-8-sig")


def build_caption(rows: list[dict[str, Any]], params: JobParams) -> str:
    title = FINISH_TITLES.get(STATE.finish_reason, "✅ Готово")
    mode = "глубокий" if params.deep_check else "быстрый"
    lines = [f"<b>{title}</b>", f"Найдено: <b>{len(rows)}</b> · режим: {mode}", ""]
    for row in rows[:CAPTION_PREVIEW]:
        lines.append(ig_link(row["username"]))
    if len(rows) > CAPTION_PREVIEW:
        lines.append(f"<i>…и ещё {len(rows) - CAPTION_PREVIEW} в файле</i>")
    if STATE.error_text:
        lines += ["", f"<i>{html.escape(STATE.error_text[:200])}</i>"]
    return "\n".join(lines)


async def deliver_results(bot: Bot) -> None:
    params = STATE.params
    chat_id = STATE.chat_id
    if params is None or chat_id is None:
        return

    await update_status(bot, final=True)
    rows = list(STATE.results)

    if not rows:
        text = "😶 Подходящих профилей не найдено."
        if STATE.error_text:
            text += f"\n\n<i>{html.escape(STATE.error_text[:300])}</i>"
        await tg_call(lambda: bot.send_message(chat_id, text))
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"lookalike_{params.seeds[0]}_{stamp}.csv"
    document = BufferedInputFile(build_csv(rows), filename=filename)
    caption = build_caption(rows, params)
    await tg_call(lambda: bot.send_document(chat_id, document=document, caption=caption))


async def run_job(bot: Bot) -> None:
    params = STATE.params
    cl = ig_client
    try:
        if params is None or cl is None:
            raise FatalJobError("Задача или Instagram-клиент не инициализированы")
        STATE.finish_reason = await crawl(bot, cl, params)
    except FatalJobError as exc:
        logger.error("Задача остановлена: %s", exc)
        STATE.finish_reason = "error"
        STATE.error_text = str(exc)
    except asyncio.CancelledError:
        STATE.is_running = False
        raise
    except Exception as exc:
        logger.exception("Непредвиденная ошибка в задаче")
        STATE.finish_reason = "error"
        STATE.error_text = f"Внутренняя ошибка: {type(exc).__name__}: {exc}"

    if cl is not None:
        await asyncio.to_thread(save_session, cl)

    try:
        await deliver_results(bot)
    except Exception:
        logger.exception("Не удалось отправить результаты")
    finally:
        STATE.is_running = False
        STATE.task = None
        logger.info(
            "Задача завершена: %s, найдено %d, запросов %d",
            STATE.finish_reason,
            len(STATE.results),
            STATE.requests,
        )


async def start_job(bot: Bot, params: JobParams, chat_id: int) -> tuple[bool, int, str]:
    """Запускает задачу. Возвращает (ok, http_status, message)."""
    if STATE.is_running or diag_running:
        return False, 409, "Уже выполняется другая задача. Остановите её или дождитесь завершения."
    if ig_client is None:
        return False, 503, f"Instagram-клиент не готов: {ig_init_error or 'неизвестная ошибка'}"

    STATE.reset(params, chat_id)
    try:
        message = await tg_call(
            lambda: bot.send_message(chat_id, render_status(), reply_markup=stop_keyboard())
        )
    except TelegramAPIError as exc:
        STATE.is_running = False
        logger.error("Не удалось отправить статус-сообщение: %s", exc)
        return False, 502, "Не удалось отправить сообщение в Telegram. Откройте чат с ботом и нажмите /start."

    STATE.status_message_id = message.message_id
    STATE.next_status_at = time.monotonic() + STATUS_INTERVAL
    STATE.task = asyncio.create_task(run_job(bot), name="lookalike-job")
    logger.info(
        "Старт задачи: seeds=%s limit=%d keywords=%s deep=%s followers=%s..%s",
        params.seeds,
        params.max_accounts,
        params.keywords,
        params.deep_check,
        params.min_followers,
        params.max_followers,
    )
    return True, 200, "Задача запущена"


# --------------------------------------------------------------------------- #
# Telegram-хэндлеры
# --------------------------------------------------------------------------- #

router = Router(name="owner")
router.message.filter(F.from_user.id == ALLOWED_USER_ID)
router.callback_query.filter(F.from_user.id == ALLOWED_USER_ID)

fallback_router = Router(name="fallback")
fallback_router.message.filter(F.chat.type == "private")

HELP_TEXT = (
    "<b>Lookalike Parser</b> — поиск похожих аккаунтов по графу рекомендаций Instagram.\n\n"
    "Откройте Mini App кнопкой ниже или используйте команды:\n\n"
    "<code>/parse username1, username2</code>\n"
    "<code>/parse seeds=user1,user2 limit=80 kw=prod,beats deep=1 min=1000 max=50000</code>\n\n"
    "• <b>limit</b> — сколько профилей собрать (10–200)\n"
    "• <b>kw</b> — ключевые слова через запятую\n"
    "• <b>deep=1</b> — глубокий анализ (био, категория, подписчики)\n"
    "• <b>min / max</b> — диапазон подписчиков (только в глубоком режиме)\n\n"
    "/diag username — проверить, какие источники рекомендаций отвечают\n"
    "/status — показать прогресс\n"
    "/stop — остановить и выгрузить найденное"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, bot: Bot) -> None:
    if not bot_ui_ready:
        await setup_bot_ui(bot)
    text = HELP_TEXT
    if not WEBAPP_URL:
        text += "\n\n⚠️ RENDER_EXTERNAL_URL не задан — кнопка Mini App недоступна."
    if ig_client is None:
        text += f"\n\n⚠️ Instagram: {html.escape(ig_init_error or 'не инициализирован')}"
    await message.answer(text, reply_markup=webapp_keyboard())


@router.message(Command("parse"))
async def cmd_parse(message: Message, command: CommandObject, bot: Bot) -> None:
    if not command.args:
        await message.answer(
            "Укажите seed-аккаунты:\n<code>/parse username1, username2</code>\n\nПодробнее — /help"
        )
        return
    try:
        params = parse_command_args(command.args)
    except ValueError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}")
        return

    ok, _, text = await start_job(bot, params, chat_id=message.chat.id)
    if not ok:
        await message.answer(f"❌ {html.escape(text)}")


@router.message(Command("stop"))
async def cmd_stop(message: Message, bot: Bot) -> None:
    if not STATE.is_running:
        await message.answer("Нет активной задачи.")
        return
    STATE.request_cancel()
    await update_status(bot, force=True)
    await message.answer("⏳ Останавливаю, файл с найденным придёт через несколько секунд.")


diag_running = False


@router.message(Command("diag"))
async def cmd_diag(message: Message, command: CommandObject) -> None:
    """Проверяет каждый источник похожих аккаунтов по отдельности и показывает, что вернул Instagram."""
    global diag_running
    username = normalize_username(command.args or "")
    if not username:
        await message.answer("Укажите аккаунт: <code>/diag username</code>")
        return
    if STATE.is_running or diag_running:
        await message.answer("Сейчас идёт другая задача — дождитесь её завершения или /stop.")
        return
    if ig_client is None:
        await message.answer(f"❌ Instagram-клиент не готов: {html.escape(ig_init_error or 'неизвестная ошибка')}")
        return

    diag_running = True
    cl = ig_client
    try:
        await message.answer(
            f"🔬 Проверяю источники для {ig_link(username)}… "
            "С паузами 5–15 сек это займёт 1–2 минуты."
        )
        try:
            user_id = str(await asyncio.to_thread(cl.user_id_from_username, username))
        except Exception as exc:
            await message.answer(f"❌ {ig_link(username)}: {type(exc).__name__}: {html.escape(str(exc)[:200])}")
            return

        lines = [f"<b>Диагностика</b> {ig_link(username)} (pk {user_id})", ""]
        for name in RELATED_SOURCES:
            try:
                users, info = await asyncio.to_thread(run_related_source, cl, name, user_id)
            except Exception as exc:
                lines.append(f"❌ <b>{name}</b>: {type(exc).__name__}: {html.escape(str(exc)[:150])}")
                continue
            sample = ", ".join(ig_link(u.username) for u in users[:5]) or "—"
            mark = "✅" if users else "⚪️"
            lines.append(f"{mark} <b>{name}</b>: {len(users)} профилей ({html.escape(info)})\n    {sample}")

        try:
            following = await asyncio.to_thread(fetch_following, cl, user_id)
            sample = ", ".join(ig_link(u.username) for u in following[:5]) or "—"
            lines.append(f"↪️ <b>подписки</b> (запасной путь): {len(following)}\n    {sample}")
        except Exception as exc:
            lines.append(f"❌ <b>подписки</b>: {type(exc).__name__}: {html.escape(str(exc)[:150])}")

        if any("not eligible" in line.lower() for line in lines):
            lines += [
                "",
                "<i>«Not eligible for chaining» — у аккаунта отключены рекомендации похожих. "
                "Бот возьмёт его подписки и продолжит граф от них.</i>",
            ]
        await asyncio.to_thread(save_session, cl)
        await message.answer("\n".join(lines))
    finally:
        diag_running = False


@router.message(Command("status"))
async def cmd_status(message: Message, bot: Bot) -> None:
    if not STATE.is_running:
        await message.answer("Нет активной задачи. Запустите новую через Mini App или /parse.")
        return
    await update_status(bot, force=True)
    await message.answer(render_status(), reply_markup=stop_keyboard())


@router.callback_query(F.data == STOP_CALLBACK)
async def cb_stop(callback: CallbackQuery, bot: Bot) -> None:
    if not STATE.is_running:
        await callback.answer("Нет активной задачи", show_alert=False)
        return
    if STATE.cancel_requested:
        await callback.answer("Уже останавливаю…")
        return
    STATE.request_cancel()
    await callback.answer("Останавливаю и собираю файл…")
    await update_status(bot, force=True)


@fallback_router.message()
async def deny_message(message: Message) -> None:
    await message.answer("⛔ Доступ запрещён.")


@fallback_router.callback_query()
async def deny_callback(callback: CallbackQuery) -> None:
    await callback.answer("⛔ Доступ запрещён.", show_alert=True)


# --------------------------------------------------------------------------- #
# aiohttp: Mini App, API и health-check
# --------------------------------------------------------------------------- #

BOT_KEY = web.AppKey("bot", Bot)


def json_error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def handle_index(request: web.Request) -> web.StreamResponse:
    if not INDEX_PATH.exists():
        return web.Response(status=404, text="index.html not found")
    return web.FileResponse(INDEX_PATH, headers={"Cache-Control": "no-store"})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "instagram": ig_client is not None,
            "job_running": STATE.is_running,
            "found": len(STATE.results) if STATE.is_running else 0,
        }
    )


async def handle_api_start(request: web.Request) -> web.Response:
    bot = request.app[BOT_KEY]

    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return json_error("Некорректный JSON", 400)
    if not isinstance(data, dict):
        return json_error("Некорректный формат запроса", 400)

    init_data = data.get("initData") or request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        return json_error("Откройте приложение из Telegram", 401)

    try:
        web_app_data = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=init_data)
    except ValueError:
        return json_error("Подпись Telegram недействительна", 401)

    user = web_app_data.user
    if user is None or user.id != ALLOWED_USER_ID:
        logger.warning("Отклонён запуск от user_id=%s", getattr(user, "id", None))
        return json_error("Доступ запрещён", 403)

    auth_date = web_app_data.auth_date
    if auth_date.tzinfo is None:
        auth_date = auth_date.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - auth_date > INIT_DATA_MAX_AGE:
        return json_error("Сессия Mini App устарела — переоткройте приложение", 401)

    try:
        params = build_params(
            seed_accounts=data.get("seed_accounts"),
            max_accounts=data.get("max_accounts"),
            keyword_filter=data.get("keyword_filter"),
            deep_check=data.get("deep_check", False),
            min_followers=data.get("min_followers"),
            max_followers=data.get("max_followers"),
        )
    except ValueError as exc:
        return json_error(str(exc), 400)

    ok, status, message = await start_job(bot, params, chat_id=user.id)
    if not ok:
        return json_error(message, status)
    return web.json_response({"ok": True, "message": message})


def build_web_app(bot: Bot) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app[BOT_KEY] = bot
    app.router.add_get("/", handle_index)
    app.router.add_get("/index.html", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/start", handle_api_start)
    return app


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #


bot_ui_ready = False


async def setup_bot_ui(bot: Bot) -> None:
    """Команды и кнопка меню для чата владельца. До первого /start Telegram отвечает «chat not found»."""
    global bot_ui_ready
    scope = BotCommandScopeChat(chat_id=ALLOWED_USER_ID)
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть парсер"),
                BotCommand(command="parse", description="Запустить парсинг командой"),
                BotCommand(command="status", description="Прогресс текущей задачи"),
                BotCommand(command="stop", description="Остановить и выгрузить"),
                BotCommand(command="diag", description="Проверить источники похожих аккаунтов"),
                BotCommand(command="help", description="Справка"),
            ],
            scope=scope,
        )
        if WEBAPP_URL:
            await bot.set_chat_menu_button(
                chat_id=ALLOWED_USER_ID,
                menu_button=MenuButtonWebApp(text="Парсер", web_app=WebAppInfo(url=WEBAPP_URL)),
            )
        bot_ui_ready = True
    except TelegramBadRequest as exc:
        if "chat not found" in str(exc).lower():
            logger.info("Меню бота настроится после первого /start от владельца")
        else:
            logger.warning("Не удалось настроить меню бота: %s", exc)
    except TelegramAPIError as exc:
        logger.warning("Не удалось настроить меню бота: %s", exc)


KEEPALIVE_INTERVAL = 10 * 60


async def keep_alive(url: str, interval: float = KEEPALIVE_INTERVAL) -> None:
    """
    Render Free усыпляет сервис через 15 минут без ВХОДЯЩИХ HTTP-запросов. Long polling —
    исходящие запросы и не считается, поэтому бот засыпал даже посреди парсинга.
    Пинг собственного публичного URL идёт через внешний прокси Render и считается входящим.
    """
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(interval)
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning("Keep-alive %s: HTTP %s", url, resp.status)
            except Exception as exc:
                logger.warning("Keep-alive %s: %s: %s", url, type(exc).__name__, exc)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    if not ALLOWED_USER_ID:
        raise SystemExit("ALLOWED_USER_ID не задан")

    init_instagram()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(fallback_router)

    runner = web.AppRunner(build_web_app(bot), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Веб-сервер запущен на порту %d, Mini App: %s", PORT, WEBAPP_URL or "—")

    keepalive_task = None
    if RENDER_EXTERNAL_URL:
        keepalive_task = asyncio.create_task(keep_alive(f"{RENDER_EXTERNAL_URL}/health"), name="keep-alive")
        logger.info("Keep-alive: пинг %s/health каждые %d мин", RENDER_EXTERNAL_URL, KEEPALIVE_INTERVAL // 60)

    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            logger.error("Telegram отклонил BOT_TOKEN (Unauthorized) — проверьте токен у @BotFather")
            raise SystemExit(1)
        logger.info("Авторизован как @%s", me.username)
        await setup_bot_ui(bot)
        # Не выбрасываем сообщения, пришедшие во время сна/рестарта — они обработаются после старта
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Бот запущен (long polling)")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if keepalive_task:
            keepalive_task.cancel()
        if STATE.task and not STATE.task.done():
            STATE.task.cancel()
        if ig_client is not None:
            save_session(ig_client)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
