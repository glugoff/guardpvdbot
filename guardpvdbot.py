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


### ========== ПЕРЕМЕННЫЕ ========== ###
BOT_TOKEN_ENVVAR = "BOT_TOKEN"   # имя переменной окружения

CHECK_MESSAGE = "Текст привратного бота для проверки на человечность"
APPROVED_MESSAGE = "Ваша заявка одобрена — добро пожаловать!"
DECLINED_MESSAGE = "Ваша заявка отклонена."
EXPIRED_MESSAGE = "Ваша заявка отклонена (нет ответа в установленный срок)."

EXPIRATION_DAYS = 7  # X дней

ADMIN_ID = 865129371  # твой Telegram ID
GROUP_ID = 5014041559  # ID тестовой группы

DB_PATH = "guardpvdbot.sqlite"
LOG_LEVEL = logging.INFO
### ================================= ###


# Логирование
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("guardpvdbot")

# Токен
TOKEN = os.getenv(BOT_TOKEN_ENVVAR)
if not TOKEN:
    raise RuntimeError("❌ Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=TOKEN)
dp = Dispatcher()


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
        await db.execute(
            "UPDATE users_requests SET status = ? WHERE user_id = ?",
            (status, user_id),
        )
        await db.commit()


async def mark_notified(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users_requests SET notified = 1 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def get_request(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, chat_id, username, request_time, status, notified FROM users_requests WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return row


async def add_message_db(user_id: int, text: str):
    ts = int(datetime.utcnow().timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, text, time) VALUES (?, ?, ?)",
            (user_id, text, ts),
        )
        await db.commit()


async def get_pending_older_than(days: int):
    cutoff = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, chat_id FROM users_requests WHERE status = 'pending' AND request_time <= ?",
            (cutoff,),
        )
        return await cur.fetchall()


# ---------- UI helpers ----------
def make_decision_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"accept:{user_id}")
    kb.button(text="❌ Отклонить", callback_data=f"reject:{user_id}")
    kb.adjust(2)
    return kb.as_markup()


# ---------- Handlers ----------
@dp.chat_join_request()
async def handle_join_request(event: ChatJoinRequest):
    user = event.from_user
    uid = user.id
    uname = user.username or user.full_name
    chat = event.chat

    logger.info(f"Join request from {uname} ({uid}) to chat {chat.id}")

    await add_request(uid, chat.id, uname)

    try:
        await bot.send_message(uid, CHECK_MESSAGE)
        logger.info(f"Sent check message to {uid}")
    except Exception as e:
        logger.exception(f"Failed to send check message to {uid}: {e}")
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Не удалось отправить проверочное сообщение пользователю {uid}: {e}",
        )


@dp.message()
async def handle_private_message(message: types.Message):
    if message.chat.type != "private":
        return

    uid = message.from_user.id
    req = await get_request(uid)
    if not req:
        logger.info(f"Message from {uid} ignored — no pending request.")
        return

    _, chat_id, username, request_time, status, notified = req
    if status != "pending":
        logger.info(f"Message from {uid} ignored — status {status}.")
        return

    # сохраняем
    text = message.text or "<non-text message>"
    await add_message_db(uid, text)

    user_label = (
        f"{message.from_user.full_name} (@{message.from_user.username})"
        if message.from_user.username
        else message.from_user.full_name
    )

    header = (
        f"Новое сообщение от кандидата\n"
        f"Пользователь: {user_label}\n"
        f"ID: {uid}\n\nТекст:"
    )

    try:
        await bot.send_message(ADMIN_ID, header, reply_markup=make_decision_kb(uid))
        await bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
    except Exception as e:
        logger.exception(f"Failed to forward message: {e}")
        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка пересылки сообщения от {uid}: {e}")

    if not notified:
        await mark_notified(uid)


# ---------- Callback handler ----------
@dp.callback_query(lambda c: c.data.startswith("accept:") or c.data.startswith("reject:"))
async def on_decision_callback(callback: types.CallbackQuery):
    actor = callback.from_user.id
    if actor != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return

    action, user_id_str = callback.data.split(":")
    user_id = int(user_id_str)

    req = await get_request(user_id)
    if not req:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    _, chat_id, username, request_time, status, notified = req

    if status != "pending":
        await callback.answer(f"Уже обработано: {status}", show_alert=True)
        return

    if action == "accept":
        try:
            await bot.approve_chat_join_request(chat_id, user_id)
            await set_status(user_id, "approved")
            await bot.send_message(user_id, APPROVED_MESSAGE)
            await callback.message.edit_text(f"Заявка {user_id} — принято ✅")
            await callback.answer("Принято.")
        except Exception as e:
            logger.exception(e)
            await callback.answer("Ошибка.", show_alert=True)

    elif action == "reject":
        try:
            await bot.decline_chat_join_request(chat_id, user_id)
            await set_status(user_id, "declined")
            await bot.send_message(user_id, DECLINED_MESSAGE)
            await callback.message.edit_text(f"Заявка {user_id} — отклонено ❌")
            await callback.answer("Отклонено.")
        except Exception as e:
            logger.exception(e)
            await callback.answer("Ошибка.", show_alert=True)


# ---------- Автоотклонение ----------
async def auto_decline_task():
    while True:
        try:
            rows = await get_pending_older_than(EXPIRATION_DAYS)
            if rows:
                logger.info(f"Auto-decline: {len(rows)} заявок.")

            for user_id, chat_id in rows:
                try:
                    await bot.decline_chat_join_request(chat_id, user_id)
                    await set_status(user_id, "expired")
                    try:
                        await bot.send_message(user_id, EXPIRED_MESSAGE)
                    except:
                        pass
                    logger.info(f"Auto-declined {user_id}")
                except Exception as e:
                    logger.exception(f"Failed auto-decline for {user_id}: {e}")

        except Exception as e:
            logger.exception(f"Auto-decline loop error: {e}")

        await asyncio.sleep(3600)  # раз в час


# ---------- Запуск ----------
async def main():
    await init_db()
    asyncio.create_task(auto_decline_task())
    logger.info("🤖 Бот запущен и ожидает событий...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
    finally:
        try:
            asyncio.run(bot.session.close())
        except:
            pass
