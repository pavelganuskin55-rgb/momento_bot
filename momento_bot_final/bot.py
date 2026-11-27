"""Telegram-бот цветочного магазина МОМЕНТО (aiogram 3), финальная версия."""

import asyncio
import csv
import html
import io
import json
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("momento_bot")


# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

def env_str(name: str, default: str = "") -> str:
    """Строка из .env; пустое значение считается незаданным."""
    return (os.getenv(name) or default).strip()


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} в .env должен быть целым числом, сейчас: {raw!r}") from e
    if value < 0:
        raise RuntimeError(f"{name} в .env не может быть отрицательным, сейчас: {value}")
    return value


BOT_TOKEN = env_str("BOT_TOKEN")
ADMIN_CHAT_ID_RAW = env_str("ADMIN_CHAT_ID")
WEBAPP_URL = env_str("WEBAPP_URL")
ORDERS_WEBHOOK_URL = env_str("ORDERS_WEBHOOK_URL")
ORDERS_WEBHOOK_SECRET = env_str("ORDERS_WEBHOOK_SECRET", "momento_secret_2026")
ASSORTMENT_CSV_URL = env_str("ASSORTMENT_CSV_URL")
DELIVERY_FEE = env_int("DELIVERY_FEE", 400)
FREE_DELIVERY_FROM = env_int("FREE_DELIVERY_FROM", 5000)
PICKUP_ADDRESS = env_str("PICKUP_ADDRESS", "ул. Примерная, д. 10 (ежедневно 07:00–22:00)")
TZ_NAME = env_str("TIMEZONE", "Europe/Moscow")

# Цены за 1 розу в конструкторе «Собрать розы поштучно» (должны совпадать с docs/index.html)
CUSTOM_ROSE_PRICES = {
    "красные розы": ("Красные розы", 180),
    "белые и кремовые": ("Белые и кремовые", 190),
    "пионовидные": ("Пионовидные", 260),
}
CUSTOM_ROSE_NAME_RE = re.compile(r"^(.+?)\s*\((\d{1,3})\s*шт\)$", re.IGNORECASE)

try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except (ZoneInfoNotFoundError, ValueError):
    logger.warning("Часовой пояс %r не найден, использую UTC+3.", TZ_NAME)
    LOCAL_TZ = timezone(timedelta(hours=3))

TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")
HTTPS_ASCII_RE = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
ACK_WORDS = frozenset({
    "ок", "окей", "ok", "okay", "спасибо", "спс", "благодарю",
    "понял", "поняла", "хорошо", "ладно", "ясно", "супер", "отлично", "+", "👍",
})

FAILED_ORDERS_FILE = "failed_orders.jsonl"
STATUS_PREFIX = "<b>Статус:</b>"
QUESTION_COOLDOWN_SEC = 5.0
CATALOG_TTL_SEC = 60.0

router = Router()

ADMIN_CHAT_ID: Optional[int] = None

_background_tasks: set[asyncio.Task] = set()

_status_locks: dict[str, asyncio.Lock] = {}
_order_rank: dict[str, int] = {}
_last_question: dict[int, float] = {}
_catalog_cache: dict[str, Any] = {"ts": 0.0, "data": None}


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def esc(value: Any) -> str:
    """Экранирование для Telegram HTML (нужны только & < >)."""
    return html.escape(str(value if value is not None else "").strip(), quote=False)


def text_of(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def one_line(value: Any, max_len: int) -> str:
    s = CTRL_RE.sub(" ", text_of(value))
    return " ".join(s.split())[:max_len]


def multi_line(value: Any, max_len: int, max_lines: int = 5) -> str:
    s = text_of(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [one_line(part, max_len) for part in s.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines[:max_lines])[:max_len]


def to_int(value: Any, min_v: int, max_v: int) -> Optional[int]:
    """Строгое преобразование в целое. None, если значение неподходящее."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        num = value
    elif isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        num = int(value)
    elif isinstance(value, str):
        s = value.strip().replace(" ", "").replace("\u00a0", "")
        if not re.fullmatch(r"\d{1,9}", s):
            return None
        num = int(s)
    else:
        return None
    return num if min_v <= num <= max_v else None


def _reject_constant(name: str):
    raise ValueError(f"недопустимая константа JSON: {name}")


def contact_html(user, user_id: int) -> str:
    username = user.username if user and user.username else ""
    if username:
        return f"@{esc(username)}"
    return f'<a href="tg://user?id={user_id}">Написать клиенту</a>'


def _spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _prune_status_caches() -> None:
    """Ограничивает рост словарей в памяти, если старые заказы не были закрыты."""
    if len(_status_locks) > 1000:
        for oid in list(_status_locks.keys())[:500]:
            if not _status_locks[oid].locked():
                _status_locks.pop(oid, None)
                _order_rank.pop(oid, None)


# --------------------------------------------------------------------------- #
# Каталог (публичный CSV) + конструктор роз поштучно
# --------------------------------------------------------------------------- #

def _parse_price(raw: Any) -> Optional[int]:
    s = re.sub(r"[^\d.,]", "", str(raw or "")).replace(",", ".")
    if not s:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return int(round(value)) if value >= 1 else None


def parse_assortment_csv(text: str) -> dict:
    by_id: dict = {}
    by_name: dict = {}
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for raw_row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        name = row.get("name", "")
        price = _parse_price(row.get("price"))
        if not name or price is None:
            continue
        entry = {
            "name": name,
            "price": price,
            "in_stock": row.get("in_stock", "").upper() in ("TRUE", "ИСТИНА", "1", "YES", "Y", "ДА", "+"),
        }
        if row.get("id"):
            by_id[row["id"].casefold()] = entry
        by_name[name.casefold()] = entry
    return {"by_id": by_id, "by_name": by_name}


async def load_catalog() -> Optional[dict]:
    """Каталог из ASSORTMENT_CSV_URL (кэш 60 с). None — проверка отключена или недоступна."""
    if not ASSORTMENT_CSV_URL:
        return None
    now = time.monotonic()
    if _catalog_cache["data"] is not None and now - _catalog_cache["ts"] < CATALOG_TTL_SEC:
        return _catalog_cache["data"]
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(ASSORTMENT_CSV_URL) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding="utf-8")
        data = parse_assortment_csv(text)
        if not data["by_name"]:
            raise ValueError("в CSV нет ни одной корректной позиции")
        _catalog_cache["data"] = data
        _catalog_cache["ts"] = now
        return data
    except Exception as e:
        logger.warning("Не удалось загрузить каталог (%s). Использую прошлую копию или цены клиента.", e)
        return _catalog_cache["data"]


def _verify_custom_rose_item(it: dict) -> Optional[bool]:
    """Проверяет и пересчитывает цену для роз поштучно.
    Возвращает True (валидный поштучный букет), False (некорректный), None (обычный букет).
    """
    m = CUSTOM_ROSE_NAME_RE.match(it["name"])
    is_custom_id = it.get("id", "").startswith("custom-")
    if not m and not is_custom_id:
        return None
    if not m:
        return False
    sort_key = m.group(1).strip().casefold()
    cnt = int(m.group(2))
    if sort_key not in CUSTOM_ROSE_PRICES or not (1 <= cnt <= 301):
        return False
    canonical_sort, unit_price = CUSTOM_ROSE_PRICES[sort_key]
    it["name"] = f"{canonical_sort} ({cnt} шт)"
    it["price"] = cnt * unit_price
    return True


def apply_catalog(items: list, catalog: dict) -> Optional[str]:
    """Подставляет цены из каталога (и пересчитывает поштучные розы). Возвращает ошибку или None."""
    for it in items:
        custom_res = _verify_custom_rose_item(it)
        if custom_res is True:
            continue
        if custom_res is False:
            return f"Некорректные параметры букета «{esc(it['name'])}». Соберите букет заново."

        entry = None
        if it.get("id"):
            entry = catalog["by_id"].get(it["id"].casefold())
        if entry is None:
            entry = catalog["by_name"].get(it["name"].casefold())
        if entry is None:
            return (
                f"Позиция «{esc(it['name'])}» не найдена в каталоге. "
                "Обновите витрину и оформите заказ заново."
            )
        if not entry["in_stock"]:
            return f"К сожалению, «{esc(entry['name'])}» сейчас нет в наличии."
        it["price"] = entry["price"]
        it["name"] = entry["name"]
    return None


# --------------------------------------------------------------------------- #
# Валидация заказа
# --------------------------------------------------------------------------- #

def parse_order_payload(raw_json: str, catalog: Optional[dict] = None):
    """Возвращает (order, None) или (None, текст_ошибки_для_клиента)."""
    generic_error = "Не удалось распознать данные заказа. Попробуйте оформить заново."
    try:
        raw = json.loads(raw_json, parse_constant=_reject_constant)
    except Exception:
        return None, generic_error
    if not isinstance(raw, dict):
        return None, generic_error

    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not (1 <= len(raw_items) <= 30):
        return None, "Корзина пуста или содержит слишком много позиций."

    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            return None, generic_error
        name = one_line(it.get("name"), 100)
        item_id = one_line(it.get("id"), 50)
        qty_raw = it.get("qty")
        qty = 1 if qty_raw is None else to_int(qty_raw, 1, 50)
        price = to_int(it.get("price"), 1, 500_000)
        if not name or qty is None or price is None:
            return None, generic_error
        items.append({"id": item_id, "name": name, "qty": qty, "price": price})

    if catalog is not None:
        error = apply_catalog(items, catalog)
        if error:
            return None, error
    else:
        # Даже без CSV пересчитываем цену поштучных роз на стороне бота
        for it in items:
            if _verify_custom_rose_item(it) is False:
                return None, f"Некорректные параметры букета «{esc(it['name'])}»."

    mode = "delivery" if raw.get("mode") == "delivery" else "pickup"

    name_client = one_line(raw.get("name"), 80)
    if not name_client:
        return None, "Укажите ваше имя при оформлении заказа."

    phone = one_line(raw.get("phone"), 30)
    digits = re.sub(r"\D", "", phone)
    if not (10 <= len(digits) <= 15):
        return None, "Укажите корректный номер телефона, чтобы флорист мог с вами связаться."

    if mode == "delivery":
        address = one_line(raw.get("address"), 220)
        if len(address) < 5:
            return None, "Укажите адрес доставки."
    else:
        address = PICKUP_ADDRESS

    subtotal = sum(i["price"] * i["qty"] for i in items)
    delivery_fee = DELIVERY_FEE if (mode == "delivery" and subtotal < FREE_DELIVERY_FROM) else 0

    recipient_name = one_line(raw.get("recipientName"), 80)
    recipient_phone = one_line(raw.get("recipientPhone"), 30)
    anonymous = bool(raw.get("anonymous"))

    return {
        "name": name_client,
        "phone": phone,
        "recipientName": recipient_name,
        "recipientPhone": recipient_phone,
        "anonymous": anonymous,
        "mode": mode,
        "address": address,
        "cardText": multi_line(raw.get("cardText"), 250),
        "items": items,
        "subtotal": subtotal,
        "deliveryFee": delivery_fee,
        "total": subtotal + delivery_fee,
    }, None


# --------------------------------------------------------------------------- #
# Google Таблица через Apps Script Webhook (без Google Cloud и MFA)
# --------------------------------------------------------------------------- #

def sheets_enabled() -> bool:
    return bool(ORDERS_WEBHOOK_URL) and ORDERS_WEBHOOK_URL.startswith("https://")


def _dump_failed_order(row: list) -> None:
    """Резервная копия заказа, если таблица недоступна (файл добавлен в .gitignore)."""
    try:
        with open(FAILED_ORDERS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("Не удалось сохранить резервную копию заказа: %s", e)


async def _post_to_sheets(payload: dict) -> bool:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=15)
    for attempt in (1, 2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(ORDERS_WEBHOOK_URL, json=payload, allow_redirects=True) as resp:
                    resp.raise_for_status()
                    text = await resp.text()
                    data = json.loads(text)
                    if data.get("ok"):
                        return True
                    logger.error("Apps Script вернул ошибку (попытка %s): %s", attempt, data)
        except Exception as e:
            logger.error("Ошибка отправки в Google Таблицу (попытка %s): %s", attempt, e)
        if attempt == 1:
            await asyncio.sleep(1.5)
    return False


async def append_order_async(order_id: str, user_id: int, username: str, order: dict) -> None:
    items_text = ", ".join(f"{i['name']} x{i['qty']}" for i in order["items"])
    row = [
        order_id,
        now_local().strftime("%d.%m.%Y %H:%M"),
        order["name"],
        order["phone"],
        f"@{username}" if username else f"id:{user_id}",
        "Доставка" if order["mode"] == "delivery" else "Самовывоз",
        order["address"],
        " | ".join(filter(None, [
            order["cardText"].replace("\n", " / "),
            f"Получатель: {order['recipientName']} {order['recipientPhone']}".strip() if order.get("recipientName") else "",
            "АНОНИМНО" if order.get("anonymous") else "",
        ])) or "-",
        items_text,
        order["deliveryFee"],
        order["total"],
        "Новый",
    ]
    ok = await _post_to_sheets({
        "secret": ORDERS_WEBHOOK_SECRET,
        "action": "append",
        "row": row,
    })
    if not ok:
        logger.error("Заказ #%s НЕ записан в таблицу, сохраняю в %s", order_id, FAILED_ORDERS_FILE)
        _dump_failed_order(row)


async def update_status_async(order_id: str, new_status_plain: str) -> None:
    await _post_to_sheets({
        "secret": ORDERS_WEBHOOK_SECRET,
        "action": "update_status",
        "order_id": order_id,
        "status": new_status_plain,
    })


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #

def build_shop_keyboard() -> Optional[ReplyKeyboardMarkup]:
    if not WEBAPP_URL or not HTTPS_ASCII_RE.match(WEBAPP_URL):
        return None
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Открыть магазин MOMENT FLO",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ],
        resize_keyboard=True,
    )


def build_status_keyboard(order_id: str, buyer_id: int, mode: str) -> InlineKeyboardMarkup:
    m = "d" if mode == "delivery" else "p"

    def btn(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"st:{action}:{m}:{order_id}:{buyer_id}")

    rows = [[btn("Взять в работу", "work"), btn("Собран", "ready")]]
    if m == "d":
        rows.append([btn("Курьер выехал", "courier"), btn("Выполнен", "done")])
    else:
        rows.append([btn("Выдан клиенту", "done")])
    rows.append([btn("Отменить", "cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer(
            f"Бот работает. ID этой группы: <code>{message.chat.id}</code>\n"
            "Открытие витрины доступно покупателям в личном чате с ботом."
        )
        return

    kb = build_shop_keyboard()
    if not kb:
        await message.answer(
            "Витрина временно настраивается (проверьте WEBAPP_URL в .env).\n"
            f"Ваш ID: <code>{message.chat.id}</code>"
        )
        return

    await message.answer(
        "Добро пожаловать в цветочный магазин <b>MOMENT FLO</b>.\n\n"
        "Нажмите кнопку ниже, чтобы открыть витрину с актуальными букетами и оформить заказ:",
        reply_markup=kb,
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


# --------------------------------------------------------------------------- #
# Приём заказа из Mini App
# --------------------------------------------------------------------------- #

@router.message(F.chat.type == "private", F.web_app_data)
async def handle_webapp_order(message: Message, bot: Bot) -> None:
    if ADMIN_CHAT_ID is None:
        logger.error("ADMIN_CHAT_ID не задан — заказ не может быть принят.")
        await message.answer("Магазин временно не принимает заказы. Напишите нам позже.")
        return

    catalog = await load_catalog()
    order, error = parse_order_payload(message.web_app_data.data, catalog)
    if order is None:
        logger.warning("Заказ отклонён: %s", error)
        await message.answer(error)
        return

    order_id = f"M-{now_local().strftime('%y%m%d')}-{secrets.token_hex(3).upper()}"
    buyer = message.from_user
    buyer_id = buyer.id if buyer else 0
    username = buyer.username if buyer and buyer.username else ""

    items_lines = ""
    for i in order["items"]:
        row = f"• {esc(i['name'])} × {i['qty']} — {i['price'] * i['qty']} ₽"
        if len(items_lines) + len(row) + 1 > 2600:
            items_lines += "\n• … (часть позиций сокращена)"
            break
        items_lines = f"{items_lines}\n{row}" if items_lines else row

    is_delivery = order["mode"] == "delivery"
    mode_label = "Доставка" if is_delivery else "Самовывоз"
    if is_delivery:
        fee_text = "Бесплатно" if order["deliveryFee"] == 0 else f"{order['deliveryFee']} ₽"
        delivery_line = f"\n<b>Доставка:</b> {fee_text}"
        address_line = f"\n<b>Адрес и время:</b> {esc(order['address'])}"
    else:
        delivery_line = ""
        address_line = ""
    recipient_line = (
        f"\n<b>Получатель:</b> {esc(order['recipientName'])} (<code>{esc(order['recipientPhone'])}</code>)"
        if order.get("recipientName")
        else ""
    )
    anon_line = "\n<b>Анонимная доставка:</b> Да (не говорить от кого)" if order.get("anonymous") else ""
    card_line = f"\n<b>Открытка:</b> {esc(order['cardText'])}" if order["cardText"] else ""

    admin_text = (
        f"<b>НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n"
        f"<b>Клиент:</b> {esc(order['name'])} ({contact_html(buyer, buyer_id)})\n"
        f"<b>Телефон:</b> <code>{esc(order['phone'])}</code>\n"
        f"<b>Способ:</b> {mode_label}"
        f"{address_line}"
        f"{recipient_line}"
        f"{anon_line}"
        f"{card_line}\n\n"
        f"<b>Состав заказа:</b>\n{items_lines}"
        f"{delivery_line}\n\n"
        f"<b>Итого:</b> {order['total']} ₽\n"
        f"{STATUS_PREFIX} Новый"
    )

    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_text,
            reply_markup=build_status_keyboard(order_id, buyer_id, order["mode"]),
        )
    except Exception as e:
        logger.exception("Сбой отправки заказа #%s в чат флориста %s: %s", order_id, ADMIN_CHAT_ID, e)
        await message.answer(
            "Извините, произошёл технический сбой при отправке заказа флористу. "
            "Пожалуйста, повторите попытку через минуту или напишите нам напрямую."
        )
        return

    if sheets_enabled():
        _spawn_background(append_order_async(order_id, buyer_id, username, order))

    where_line = f"\n<b>Самовывоз:</b> {esc(order['address'])}" if not is_delivery else ""
    buyer_text = (
        f"Спасибо! Ваш заказ <b>#{order_id}</b> принят.\n\n"
        f"{items_lines}"
        f"{delivery_line}"
        f"{where_line}\n"
        f"<b>Итого:</b> {order['total']} ₽\n\n"
        "Флорист уже получил заявку и скоро свяжется с вами."
    )
    try:
        await message.answer(buyer_text)
    except Exception as e:
        logger.warning("Не удалось отправить подтверждение клиенту (заказ #%s): %s", order_id, e)


# --------------------------------------------------------------------------- #
# Смена статуса заказа
# --------------------------------------------------------------------------- #

STATUS_RANK = {"work": 1, "ready": 2, "courier": 3, "done": 4, "cancel": 5}
LABEL_RANKS = (
    ("Новый", 0),
    ("В работе", 1),
    ("Собран", 2),
    ("Передан курьеру", 3),
    ("Выполнен", 4),
    ("Отменён", 5),
)


def rank_of_label(label: str) -> int:
    for prefix, rank in LABEL_RANKS:
        if label.startswith(prefix):
            return rank
    return 0


def find_status_line(lines: list) -> Optional[int]:
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].startswith(STATUS_PREFIX):
            return idx
    return None


@router.callback_query(F.data.startswith("st:"))
async def handle_status_change(callback: CallbackQuery, bot: Bot) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Сообщение устарело и недоступно для редактирования.", show_alert=True)
        return

    if ADMIN_CHAT_ID is not None and callback.message.chat.id != ADMIN_CHAT_ID:
        await callback.answer("Нет прав для изменения статуса.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, action, mode_code, order_id, buyer_id_str = parts
    if action not in STATUS_RANK:
        await callback.answer("Неизвестный статус.", show_alert=True)
        return
    try:
        buyer_id = int(buyer_id_str)
    except ValueError:
        await callback.answer("Ошибка ID покупателя.", show_alert=True)
        return

    mode = "delivery" if mode_code == "d" else "pickup"
    florist_plain = one_line(callback.from_user.first_name, 40) or "Флорист"
    florist_html = esc(florist_plain)

    labels = {
        "work": (f"В работе ({florist_plain})", f"В работе ({florist_html})",
                 f"Флорист уже собирает ваш заказ #{order_id}."),
        "ready": (f"Собран ({florist_plain})", f"Собран ({florist_html})",
                  f"Ваш букет по заказу #{order_id} собран и готов"
                  + (" к выдаче!" if mode == "pickup" else "!")),
        "courier": ("Передан курьеру", "Передан курьеру",
                    f"Курьер выехал с вашим заказом #{order_id}."),
        "done": ("Выполнен", "Выполнен",
                 f"Заказ #{order_id} выполнен. Спасибо, что выбрали MOMENT FLO!"),
        "cancel": (f"Отменён ({florist_plain})", f"Отменён ({florist_html})",
                   f"Заказ #{order_id} отменён. Если есть вопросы, напишите нам."),
    }
    label_plain, label_html, buyer_msg = labels[action]
    new_rank = STATUS_RANK[action]

    _prune_status_caches()
    lock = _status_locks.setdefault(order_id, asyncio.Lock())
    async with lock:
        lines = (callback.message.html_text or "").splitlines()
        idx = find_status_line(lines)
        if idx is None:
            await callback.answer("В карточке не найдена строка статуса.", show_alert=True)
            return

        current_label = html.unescape(lines[idx][len(STATUS_PREFIX):]).strip()
        current_rank = max(rank_of_label(current_label), _order_rank.get(order_id, 0))

        if current_rank >= 4:
            await callback.answer("Заказ уже завершён.", show_alert=True)
            return
        if action != "cancel" and new_rank <= current_rank:
            await callback.answer("Этот статус уже установлен или пройден.")
            return

        lines[idx] = f"{STATUS_PREFIX} {label_html}"
        kb = None if action in ("done", "cancel") else build_status_keyboard(order_id, buyer_id, mode)

        try:
            await callback.message.edit_text("\n".join(lines), reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer("Этот статус уже установлен.")
            else:
                logger.warning("Ошибка edit_text для #%s: %s", order_id, e)
                await callback.answer("Не удалось обновить карточку.", show_alert=True)
            return

        _order_rank[order_id] = new_rank
        if action in ("done", "cancel"):
            _status_locks.pop(order_id, None)
            _order_rank.pop(order_id, None)

    if sheets_enabled():
        _spawn_background(update_status_async(order_id, label_plain))

    if buyer_id and buyer_msg:
        try:
            await bot.send_message(chat_id=buyer_id, text=buyer_msg)
        except Exception as e:
            logger.warning("Не удалось уведомить покупателя %s: %s", buyer_id, e)
            await callback.answer(f"Статус: {label_plain}. Клиента уведомить не удалось.", show_alert=True)
            return

    await callback.answer(f"Статус: {label_plain}")


# --------------------------------------------------------------------------- #
# Служебное: группа стала супергруппой
# --------------------------------------------------------------------------- #

@router.message(F.migrate_to_chat_id)
async def handle_group_migration(message: Message) -> None:
    logger.error(
        "Группа %s стала супергруппой. Новый ID: %s — обновите ADMIN_CHAT_ID в .env и перезапустите бота.",
        message.chat.id,
        message.migrate_to_chat_id,
    )


# --------------------------------------------------------------------------- #
# Текстовые сообщения клиентов в личке
# --------------------------------------------------------------------------- #

async def forward_question_to_admin(message: Message, bot: Bot) -> str:
    """Пересылает свободный вопрос флористу. Возвращает 'sent' | 'throttled' | 'off'."""
    if ADMIN_CHAT_ID is None or not message.from_user:
        return "off"

    user = message.from_user
    now = time.monotonic()
    last = _last_question.get(user.id, 0.0)
    if now - last < QUESTION_COOLDOWN_SEC:
        return "throttled"
    if len(_last_question) > 2000:
        _last_question.clear()
    _last_question[user.id] = now

    text = (
        "<b>Вопрос от клиента</b>\n"
        f"{esc(user.full_name)} ({contact_html(user, user.id)})\n\n"
        f"{esc(multi_line(message.text, 1000, max_lines=20))}"
    )
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        return "sent"
    except Exception as e:
        logger.warning("Не удалось переслать вопрос клиента %s флористу: %s", user.id, e)
        return "off"


@router.message(F.chat.type == "private", F.text)
async def handle_private_text(message: Message, bot: Bot) -> None:
    text = (message.text or "").strip()
    lowered = text.lower()
    norm = re.sub(r"[.!?,)\s]+$", "", lowered)

    # Короткие благодарности и «ок» не шлём флористу как вопрос
    if not norm or norm in ACK_WORDS:
        return

    kb = build_shop_keyboard()

    if text.startswith("/"):
        reply = "Чтобы выбрать и заказать цветы, нажмите кнопку «Открыть магазин MOMENT FLO» ниже."
    elif re.search(r"\b(доставк\w*|курьер\w*|привез\w*)\b", lowered):
        reply = (
            f"Доставляем по городу от 60 минут. Стоимость — {DELIVERY_FEE} ₽, "
            f"при заказе от {FREE_DELIVERY_FROM} ₽ — бесплатно.\n\n"
            "Оформить доставку можно в каталоге по кнопке ниже."
        )
    elif re.search(r"\b(график\w*|режим\w*|часы работы|со скольки|до скольки)\b", lowered):
        reply = "Работаем ежедневно с 07:00 до 22:00. Заказы через витрину принимаем круглосуточно."
    elif re.search(r"\b(адрес\w*|самовывоз\w*|откуда забирать)\b", lowered):
        reply = f"Самовывоз: {esc(PICKUP_ADDRESS)}."
    else:
        result = await forward_question_to_admin(message, bot)
        if result == "sent":
            reply = (
                "Передали ваше сообщение флористу — ответим в ближайшее время.\n\n"
                "Актуальные цветы и цены — в витрине по кнопке ниже."
            )
        elif result == "throttled":
            reply = "Мы получили ваше предыдущее сообщение и скоро ответим."
        else:
            reply = (
                "Актуальное наличие цветов и цены на сегодня — в витрине "
                "по кнопке «Открыть магазин MOMENT FLO» ниже."
            )

    await message.answer(reply, reply_markup=kb)


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #

def validate_config() -> None:
    global ADMIN_CHAT_ID

    if not BOT_TOKEN or not TOKEN_RE.match(BOT_TOKEN):
        raise RuntimeError(
            "Укажите корректный BOT_TOKEN в .env (получить в @BotFather, формат 123456:AAH...)."
        )

    if ADMIN_CHAT_ID_RAW:
        try:
            ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
        except ValueError as e:
            raise RuntimeError(
                f"ADMIN_CHAT_ID в .env должен быть целым числом (например -1001234567890), "
                f"сейчас: {ADMIN_CHAT_ID_RAW!r}"
            ) from e
    else:
        logger.warning(
            "ADMIN_CHAT_ID не задан! Добавьте бота в группу флористов, напишите /myid, "
            "впишите ID в .env и перезапустите бота. До этого заказы приниматься не будут."
        )

    if not WEBAPP_URL or not HTTPS_ASCII_RE.match(WEBAPP_URL):
        logger.warning("WEBAPP_URL не задан или некорректен (%r) — кнопка витрины не будет показана.", WEBAPP_URL)

    if not ORDERS_WEBHOOK_URL:
        logger.warning("ORDERS_WEBHOOK_URL не задан — запись заказов в Google Таблицу отключена.")
    elif not ORDERS_WEBHOOK_URL.startswith("https://"):
        raise RuntimeError("ORDERS_WEBHOOK_URL должен начинаться с https://")
    else:
        logger.info("Запись заказов в Google Таблицу (Apps Script) включена.")

    if ASSORTMENT_CSV_URL:
        if not ASSORTMENT_CSV_URL.startswith("https://"):
            raise RuntimeError("ASSORTMENT_CSV_URL должен начинаться с https://")
        logger.info("Проверка цен и наличия по каталогу включена.")
    else:
        logger.warning("ASSORTMENT_CSV_URL не задан — цены букетов берутся из витрины (поштучные розы пересчитываются ботом).")


async def main() -> None:
    validate_config()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Бот МОМЕНТО запущен (таймзона: %s)...", TZ_NAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
