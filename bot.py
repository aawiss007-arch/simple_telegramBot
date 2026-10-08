"""Telegram-бот задач. Запуск: python bot.py."""

import asyncio
import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.exception import ExceptionTypeFilter
from aiogram.types import BotCommand, BufferedInputFile, ErrorEvent, Message
from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import load_dotenv

from csv_export import build_csv
from storage import TaskStore

PROJECT_DIR = Path(__file__).resolve().parent
HELP_TEXT = (
    "/add текст задачи — добавить задачу\n"
    "/list — показать свои задачи\n"
    "/list_csv — скачать свои задачи в CSV"
)
logger = logging.getLogger(__name__)


def load_settings() -> tuple[str, Path]:
    """Читает .env рядом с кодом и проверяет токен, не выводя его в журнал."""
    load_dotenv(PROJECT_DIR / ".env", encoding="utf-8-sig")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Укажите BOT_TOKEN в файле .env (токен от @BotFather).")
    try:
        validate_token(token)
    except TokenValidationError:
        raise ValueError("BOT_TOKEN имеет неверный формат. Проверьте файл .env.") from None
    path = Path(os.getenv("DB_PATH", "").strip() or "data/tasks.db").expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return token, path


def split_messages(text: str, limit: int = 4000) -> Iterator[str]:
    """Делит текст с учётом UTF-16, чтобы длинные списки и emoji помещались."""
    buffer: list[str] = []
    units = 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > limit:
            yield "".join(buffer)
            buffer = []
            units = 0
        buffer.append(character)
        units += width
    if buffer:
        yield "".join(buffer)


def build_dispatcher(store: TaskStore) -> Dispatcher:
    """Регистрирует команды; SQLite выполняется в потоке вне цикла asyncio."""
    dispatcher = Dispatcher()
    router = Router(name="tasks")
    # Личный чат не раскрывает список задач другим участникам группы.
    router.message.filter(F.chat.type == "private", F.from_user)
    dispatcher.include_router(router)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        """Приветствует пользователя и показывает доступные команды."""
        await message.answer("Привет! Я сохраняю твои задачи.\n\n" + HELP_TEXT)

    @router.message(Command("add"))
    async def add(message: Message, command: CommandObject) -> None:
        """Добавляет текст после /add или показывает пример при пустом тексте."""
        text = (command.args or "").strip()
        if not text:
            await message.answer("Напиши текст после команды, например:\n/add Купить молоко")
            return
        task_id = await asyncio.to_thread(store.add_task, message.from_user.id, text)
        await message.answer(f"Задача #{task_id} добавлена.")

    @router.message(Command("list"))
    async def list_tasks(message: Message) -> None:
        """Показывает задачи владельца, разбивая длинный список на сообщения."""
        tasks = await asyncio.to_thread(store.list_tasks, message.from_user.id)
        if not tasks:
            await message.answer("Задач пока нет. Добавь первую: /add текст задачи")
            return
        text = "Твои задачи:\n\n" + "\n\n".join(
            f"{number}. [#{task.id}] {task.text}\nСоздана: {task.created_at} UTC"
            for number, task in enumerate(tasks, start=1)
        )
        for part in split_messages(text):
            await message.answer(part)

    @router.message(Command("list_csv"))
    async def list_csv(message: Message) -> None:
        """Отправляет CSV только с задачами владельца, включая пустой список."""
        tasks = await asyncio.to_thread(store.list_tasks, message.from_user.id)
        data = await asyncio.to_thread(build_csv, tasks)
        document = BufferedInputFile(data, filename="tasks.csv")
        await message.answer_document(
            document,
            caption=f"Задач: {len(tasks)}. Колонки «Статус» и «Категория» можно заполнить в таблице.",
        )

    @router.message()
    async def help_message(message: Message) -> None:
        """Подсказывает команды для неизвестного текста и вложений."""
        await message.answer("Чтобы сохранить задачу, используй /add.\n\n" + HELP_TEXT)

    @dispatcher.message(F.chat.type != "private", Command("start", "add", "list", "list_csv"))
    async def group_help(message: Message) -> None:
        """Предлагает перейти в личный чат при вызове команды в группе."""
        await message.answer("Для работы с задачами открой личный чат с ботом.")

    @dispatcher.errors(ExceptionTypeFilter(sqlite3.Error))
    async def database_error(event: ErrorEvent) -> bool:
        """Сообщает о сбое SQLite, не перехватывая ошибки других типов."""
        logger.error("Ошибка SQLite: %s", event.exception)
        if event.update.message:
            await event.update.message.answer("Не удалось обратиться к базе задач. Попробуй позже.")
        return True

    return dispatcher


async def main() -> None:
    """Создаёт таблицу, меню команд и запускает получение обновлений Telegram."""
    token, database_path = load_settings()
    store = TaskStore(database_path)
    await asyncio.to_thread(store.initialize)
    dispatcher = build_dispatcher(store)
    bot = Bot(token=token)
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Приветствие и помощь"),
            BotCommand(command="add", description="Добавить задачу: /add текст"),
            BotCommand(command="list", description="Показать свои задачи"),
            BotCommand(command="list_csv", description="Скачать задачи в CSV"),
        ])
        # Polling требует отключённого webhook; ожидающие обновления сохраняются.
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("База задач: %s", database_path)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram отклонил BOT_TOKEN. Проверьте токен в .env.") from None
    except TelegramNetworkError:
        raise SystemExit("Нет связи с Telegram. Проверьте интернет и повторите запуск.") from None
    except KeyboardInterrupt:
        pass
