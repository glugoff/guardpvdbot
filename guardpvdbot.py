#!/usr/bin/env python3
# guardpvdbot.py — main

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatJoinRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
# from aiogram.utils.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

### ========== ПЕРЕМЕННЫЕ ========== ###
# Меняй эти переменные при необходимости
BOT_TOKEN_ENVVAR = "BOT_TOKEN"   # имя переменной окружения с токеном

CHECK_MESSAGE = "Текст привратного бота для проверки на человечность"
APPROVED_MESSAGE = "Ваша заявка одобрена — добро пожаловать!"
DECLINED_MESSAGE = "Ваша заявка отклонена."
EXPIRED_MESSAGE = "Ваша заявка отклонена (нет ответа в установленный срок)."

EXPIRATION_DAYS = 7  # X дней до автоматического отклонения

ADMIN_ID = 865129371  # твой Telegram id
GROUP_ID = 5014041559  # id тестовой группы

DB_PATH = "guardpvdbot.sqlite"
LOG_LEVEL = logging.INFO
### ================================= ###

# Логирование (systemd / journalctl будет это показывать)
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("guardpvdbot")

# Получаем токен из окружения (без токена не стартуем)
TOKEN = os.getenv(BOT_TOKEN_ENVVAR)
if not TOKEN:
    raise RuntimeError("❌ Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# CallbackData для inline-кнопок: decide:action:user_id
decide_cb = CallbackData("decide", "action", "user_id")

# ---------- DB helpers ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users_requests (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                username TEXT,
                request_time INTEGER,
                status TEXT,
                notified INTEGER DEFAULT 0
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                time INTEGER
            );
            """
        )
        await db.commit()

async def add_request(user_id: int, chat_id: int, username: Optional[str]):
    ts = int(datetime.utcnow().timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO users_requests (user_id, chat_id, username, request_time, status, notified)
            VALUES (?, ?, ?, ?, 'pending', 0)
            """,
            (user_id, chat_id, username, ts),
        )
        await db.commit()

async def set_status(user_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users_requests SET status = ? WHERE user_id = ?", (status, user_id))
        await db.commit()

async def mark_notified(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users_requests SET notified = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def get_request(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, chat_id, username, request_time, status, notified FROM users_requests WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row

async def add_message_db(user_id: int, text: str):
    ts = int(datetime.utcnow().timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO messages (user_id, text, time) VALUES (?, ?, ?)", (user_id, text, ts))
        await db.commit()

async def get_pending_older_than(days: int):
    cutoff = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, chat_id FROM users_requests WHERE status = 'pending' AND request_time <= ?", (cutoff,))
        rows = await cur.fetchall()
        return rows

# ---------- UI helpers ----------
def make_decision_kb(user_id: int):
    kb_builder = InlineKeyboardBuilder()
    kb_builder.button(text="✅ Принять", callback_data=decide_cb.new(action="accept", user_id=str(user_id)))
    kb_builder.button(text="❌ Отклонить", callback_data=decide_cb.new(action="decline", user_id=str(user_id)))
    kb_builder.adjust(2)
    return kb_builder.as_markup()

# ---------- Handlers ----------
@dp.chat_join_request()
async def handle_join_request(event: ChatJoinRequest):
    user = event.from_user
    uid = user.id
    uname = user.username or user.full_name
    chat = event.chat

    logger.info(f"Join request from {uname} ({uid}) to chat {chat.id}")

    # сохраняем заявку
    await add_request(uid, chat.id, uname)

    # отправляем пользователю проверочное сообщение
    try:
        await bot.send_message(uid, CHECK_MESSAGE)
        logger.info(f"Sent check message to {uid}")
    except Exception as e:
        logger.exception(f"Failed to send check-message to {uid}: {e}")
        # уведомляем админа о проблеме
        await bot.send_message(ADMIN_ID, f"⚠️ Не удалось отправить проверочное сообщение пользователю {uid}: {e}")

@dp.message()
async def handle_private_message(message: types.Message):
    # Обрабатываем только личные сообщения от кандидатов (не группы)
    if message.chat.type != "private":
        return

    uid = message.from_user.id
    req = await get_request(uid)
    if not req:
        # если нет открытой заявки — игнорируем (или логируем)
        logger.info(f"Received message from {uid} but no pending request found. Ignored.")
        return

    _, chat_id, username, request_time, status, notified = req
    if status != "pending":
        logger.info(f"Received message from {uid} but status is {status} — ignored.")
        return

    # сохраняем текст (если есть текст)
    if message.text:
        await add_message_db(uid, message.text)
    else:
        # если медиа/стикер — пересылаем как есть, но для БД сохраняем описание
        await add_message_db(uid, "<non-text message>")

    logger.info(f"Saved message from {uid}. Forwarding to admin...")

    # отправляем админу уведомление + пересылаем сообщение
    user_label = f"{message.from_user.full_name} (@{message.from_user.username})" if message.from_user.username else message.from_user.full_name
    header = f"Новое сообщение от кандидата\nПользователь: {user_label}\nID: {uid}\n\nТекст (если есть):"
    try:
        # сначала информируем (с кнопками)
        await bot.send_message(ADMIN_ID, header, reply_markup=make_decision_kb(uid))
        # затем пересылаем оригинал (чтобы был полный контекст — все сообщения пересылаются этим способом)
        await bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
    except Exception as e:
        logger.exception(f"Failed to forward message from {uid} to admin: {e}")
        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка пересылки сообщения от {uid}: {e}")

    # пометка, что мы уведомили админа (можно ставить 1 при первом уведомлении)
    if not notified:
        await mark_notified(uid)

# ---------- Callback handler (админские кнопки) ----------
@dp.callback_query(decide_cb.filter())
async def on_decision_callback(callback: types.CallbackQuery, callback_data: dict):
    actor = callback.from_user.id
    if actor != ADMIN_ID:
        await callback.answer("Только админ может принимать решения.", show_alert=True)
        return

    action = callback_data.get("action")
    user_id = int(callback_data.get("user_id"))

    req = await get_request(user_id)
    if not req:
        await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
        return

    _, chat_id, username, request_time, status, notified = req
    if status != "pending":
        await callback.answer(f"Заявка уже обработана: {status}", show_alert=True)
        return

    if action == "accept":
        try:
            await bot.approve_chat_join_request(chat_id, user_id)
            await set_status(user_id, "approved")
            await bot.send_message(user_id, APPROVED_MESSAGE)
            await callback.message.edit_text(f"Заявка {user_id} — принято ✅")
            await callback.answer("Пользователь принят.")
            logger.info(f"User {user_id} approved by admin.")
        except Exception as e:
            logger.exception(f"Error approving {user_id}: {e}")
            await callback.answer(f"Ошибка при одобрении: {e}", show_alert=True)
    elif action == "decline":
        try:
            await bot.decline_chat_join_request(chat_id, user_id)
            await set_status(user_id, "declined")
            await bot.send_message(user_id, DECLINED_MESSAGE)
            await callback.message.edit_text(f"Заявка {user_id} — отклонено ❌")
            await callback.answer("Заявка отклонена.")
            logger.info(f"User {user_id} declined by admin.")
        except Exception as e:
            logger.exception(f"Error declining {user_id}: {e}")
            await callback.answer(f"Ошибка при отклонении: {e}", show_alert=True)
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)

# ---------- Автоотклонение просроченных заявок ----------
async def auto_decline_task():
    while True:
        try:
            rows = await get_pending_older_than(EXPIRATION_DAYS)
            if rows:
                logger.info(f"Auto-decline: found {len(rows)} expired pending requests.")
            for (user_id, chat_id) in rows:
                try:
                    await bot.decline_chat_join_request(chat_id, user_id)
                    await set_status(user_id, "expired")
                    try:
                        await bot.send_message(user_id, EXPIRED_MESSAGE)
                    except Exception:
                        logger.info(f"Cannot message expired user {user_id} (maybe blocked bot).")
                    logger.info(f"Auto-declined request {user_id}")
                except Exception as e:
                    logger.exception(f"Failed to auto-decline {user_id}: {e}")
        except Exception as e:
            logger.exception(f"Error in auto_decline_task main loop: {e}")
        # Проверяем раз в час
        await asyncio.sleep(3600)

# ---------- Запуск ----------
async def main():
    await init_db()
    # создаём таск автоотклонения
    asyncio.create_task(auto_decline_task())
    logger.info("🤖 Бот запущен и ожидает событий...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
    finally:
        asyncio.run(bot.session.close())
