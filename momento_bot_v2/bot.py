import asyncio
import html
import json
import logging
import os
import random
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "credentials.json").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("momento_bot")

router = Router()

_cached_spreadsheet = None


def _get_spreadsheet_sync():
    """Возвращает кэшированное подключение к Google Таблице."""
    global _cached_spreadsheet
    if _cached_spreadsheet is not None:
        return _cached_spreadsheet
    if not SPREADSHEET_ID or not os.path.exists(GOOGLE_CREDS_FILE):
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        _cached_spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        return _cached_spreadsheet
    except Exception as e:
        logger.warning(f"Не удалось подключиться к Google Таблице: {e}")
        return None


def _get_orders_worksheet_sync():
    sh = _get_spreadsheet_sync()
    if not sh:
        return None
    try:
        return sh.worksheet("Заказы")
    except Exception:
        ws = sh.add_worksheet(title="Заказы", rows=1000, cols=12)
        ws.append_row([
            "ID заказа",
            "Дата и время",
            "Имя",
            "Телефон",
            "Username",
            "Способ",
            "Адрес",
            "Открытка",
            "Состав",
            "Доставка",
            "Итого",
            "Статус",
        ])
        return ws


def _append_order_sync(order_id: str, user_id: int, username: str, data: dict) -> None:
    global _cached_spreadsheet
    try:
        ws = _get_orders_worksheet_sync()
        if not ws:
            return
        items_text = ", ".join(
            f"{i.get('name', '')} x{i.get('qty', 1)}" for i in data.get("items", [])
        )
        mode_text = "Доставка" if data.get("mode") == "delivery" else "Самовывоз"
        ws.append_row([
            order_id,
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            str(data.get("name", "")),
            str(data.get("phone", "")),
            f"@{username}" if username else f"id:{user_id}",
            mode_text,
            str(data.get("address", "-")),
            str(data.get("cardText", "-")),
            items_text,
            int(data.get("deliveryFee", 0)),
            int(data.get("total", 0)),
            "Новый",
        ])
    except Exception as e:
        logger.error(f"Ошибка записи заказа в Google Таблицу: {e}")
        _cached_spreadsheet = None


def _update_status_sync(order_id: str, new_status: str) -> None:
    global _cached_spreadsheet
    try:
        ws = _get_orders_worksheet_sync()
        if not ws:
            return
        cell = ws.find(order_id, in_column=1)
        if cell:
            ws.update_cell(cell.row, 12, new_status)
    except Exception as e:
        logger.error(f"Ошибка обновления статуса в Google Таблице: {e}")
        _cached_spreadsheet = None


def build_shop_keyboard() -> Optional[ReplyKeyboardMarkup]:
    if not WEBAPP_URL or not WEBAPP_URL.startswith("https://"):
        return None
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Открыть магазин МОМЕНТО",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ],
        resize_keyboard=True,
    )


def build_status_keyboard(order_id: str, buyer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Взять в работу",
                    callback_data=f"st:work:{order_id}:{buyer_id}",
                ),
                InlineKeyboardButton(
                    text="Собран",
                    callback_data=f"st:ready:{order_id}:{buyer_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Курьер выехал",
                    callback_data=f"st:courier:{order_id}:{buyer_id}",
                ),
                InlineKeyboardButton(
                    text="Выполнен",
                    callback_data=f"st:done:{order_id}:{buyer_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Отменить",
                    callback_data=f"st:cancel:{order_id}:{buyer_id}",
                ),
            ],
        ]
    )


def esc(value) -> str:
    return html.escape(str(value or "").strip())


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    kb = build_shop_keyboard()
    if not kb:
        await message.answer(
            "Бот запущен, но ссылка на витрину (WEBAPP_URL) ещё не указана в файле .env.\n"
            f"ID этого чата: <code>{message.chat.id}</code>"
        )
        return

    await message.answer(
        "Добро пожаловать в цветочный магазин <b>МОМЕНТО</b>.\n\n"
        "Нажмите кнопку ниже, чтобы открыть витрину с актуальными букетами и оформить заказ:",
        reply_markup=kb,
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    """Подсказывает ID текущего чата или группы для ADMIN_CHAT_ID."""
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@router.message(F.web_app_data)
async def handle_webapp_order(message: Message, bot: Bot) -> None:
    """Приём заказа из Telegram Mini App."""
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        await message.answer("Не удалось прочитать заказ. Попробуйте оформить ещё раз.")
        return

    items = data.get("items", [])
    if not items:
        await message.answer("Корзина пуста — заказ не оформлен.")
        return

    order_id = f"M-{datetime.now().strftime('%d%H%M%S')}-{random.randint(10, 99)}"
    buyer = message.from_user
    buyer_id = buyer.id if buyer else 0
    username = buyer.username if buyer and buyer.username else ""

    items_lines = "\n".join(
        f"• {esc(item.get('name'))} × {int(item.get('qty', 1))} — "
        f"{int(item.get('price', 0)) * int(item.get('qty', 1))} ₽"
        for item in items
    )

    is_delivery = data.get("mode") == "delivery"
    mode_label = "Доставка" if is_delivery else "Самовывоз"
    delivery_fee = int(data.get("deliveryFee", 0))
    total = int(data.get("total", 0))

    address_raw = esc(data.get("address"))
    card_raw = esc(data.get("cardText"))
    name_raw = esc(data.get("name")) or "Не указано"
    phone_raw = esc(data.get("phone")) or "Не указан"

    address_line = f"\n<b>Адрес и время:</b> {address_raw or 'не указан'}" if is_delivery else ""
    delivery_line = (
        f"\n<b>Доставка:</b> {'Бесплатно' if delivery_fee == 0 else f'{delivery_fee} ₽'}"
        if is_delivery
        else ""
    )
    card_line = f"\n<b>Открытка:</b> {card_raw}" if card_raw else ""
    contact_tg = (
        f"@{esc(username)}"
        if username
        else f'<a href="tg://user?id={buyer_id}">Написать клиенту</a>'
    )

    # 1. Подтверждение покупателю
    await message.answer(
        f"Спасибо! Ваш заказ <b>#{order_id}</b> принят.\n\n"
        f"{items_lines}"
        f"{delivery_line}\n"
        f"<b>Итого:</b> {total} ₽\n\n"
        f"Флорист уже получил заявку и скоро свяжется с вами."
    )

    # 2. Запись в Google Таблицу (в отдельном потоке, не блокируя бота)
    await asyncio.to_thread(_append_order_sync, order_id, buyer_id, username, data)

    # 3. Отправка карточки в рабочий чат флориста
    admin_text = (
        f"<b>НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n"
        f"<b>Клиент:</b> {name_raw} ({contact_tg})\n"
        f"<b>Телефон:</b> <code>{phone_raw}</code>\n"
        f"<b>Способ:</b> {mode_label}"
        f"{address_line}"
        f"{card_line}\n\n"
        f"<b>Состав заказа:</b>\n{items_lines}"
        f"{delivery_line}\n\n"
        f"<b>Итого:</b> {total} ₽\n"
        f"<b>Статус:</b> Новый"
    )

    target_chat = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else buyer_id
    try:
        await bot.send_message(
            chat_id=target_chat,
            text=admin_text,
            reply_markup=build_status_keyboard(order_id, buyer_id),
        )
    except Exception as e:
        logger.error(f"Не удалось отправить заказ #{order_id} в чат {target_chat}: {e}")


@router.callback_query(F.data.startswith("st:"))
async def handle_status_change(callback: CallbackQuery, bot: Bot) -> None:
    """Смена статуса заказа флористом с сохранением HTML-разметки и обновлением таблицы."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные кнопки", show_alert=True)
        return

    _, action, order_id, buyer_id_str = parts
    buyer_id = int(buyer_id_str)
    florist = esc(callback.from_user.first_name or "Флорист")

    status_map = {
        "work": (f"В работе ({florist})", f"Флорист уже собирает ваш заказ #{order_id}."),
        "ready": (f"Собран ({florist})", f"Ваш букет по заказу #{order_id} собран и готов!"),
        "courier": ("Передан курьеру", f"Курьер выехал с вашим заказом #{order_id}."),
        "done": ("Выполнен", f"Заказ #{order_id} выполнен. Спасибо, что выбрали МОМЕНТО!"),
        "cancel": (f"Отменён ({florist})", f"Заказ #{order_id} отменён. Если есть вопросы, напишите нам."),
    }

    label, buyer_msg = status_map.get(action, ("Обновлён", ""))

    # Используем html_text, чтобы сохранить жирное начертание и ссылку на покупателя
    if callback.message and callback.message.html_text:
        lines = callback.message.html_text.splitlines()
        new_lines = [
            f"<b>Статус:</b> {label}" if "Статус:" in line else line
            for line in lines
        ]
        updated_html = "\n".join(new_lines)
        kb = None if action in ("done", "cancel") else build_status_keyboard(order_id, buyer_id)
        try:
            await callback.message.edit_text(updated_html, reply_markup=kb)
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение заказа #{order_id}: {e}")

    # Обновляем статус в Google Таблице
    await asyncio.to_thread(_update_status_sync, order_id, label)

    # Уведомляем покупателя
    if buyer_id and buyer_msg:
        try:
            await bot.send_message(chat_id=buyer_id, text=buyer_msg)
        except Exception as e:
            logger.warning(f"Не удалось уведомить покупателя {buyer_id}: {e}")

    await callback.answer(f"Статус: {label}")


@router.message(F.chat.type == "private", F.text)
async def handle_private_text(message: Message) -> None:
    """Базовый автоответчик на частые текстовые вопросы в личке бота."""
    text = (message.text or "").lower()
    kb = build_shop_keyboard()

    if any(w in text for w in ("доставк", "курьер", "привез")):
        reply = (
            "Доставляем по городу от 60 минут. Стоимость доставки — 400 ₽, "
            "при заказе от 5 000 ₽ — бесплатно.\n\n"
            "Оформить доставку можно в нашем каталоге по кнопке ниже."
        )
    elif any(w in text for w in ("график", "режим", "работает", "время", "часы")):
        reply = (
            "Работаем ежедневно с 09:00 до 22:00. "
            "Заказы через бота принимаем круглосуточно."
        )
    elif any(w in text for w in ("адрес", "где", "самовывоз", "находит")):
        reply = "Самовывоз доступен ежедневно с 09:00 до 22:00. Точный адрес указан во вкладке «Доставка» внутри магазина."
    else:
        reply = (
            "Чтобы посмотреть актуальное наличие цветов и цены на сегодня, "
            "нажмите кнопку «Открыть магазин МОМЕНТО» ниже."
        )

    await message.answer(reply, reply_markup=kb)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в файле .env")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Бот МОМЕНТО запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
