"""Проверки с временной SQLite и Telegram-сессией без сетевых запросов."""

import codecs
import csv
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendDocument, SendMessage
from aiogram.types import BufferedInputFile, Chat, Message, Update, User

from bot import PROJECT_DIR, build_dispatcher, load_settings, split_messages
from csv_export import build_csv
from storage import Task, TaskStore


def read_csv(data):
    """Разбирает CSV теми же параметрами, которые используются при импорте."""
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))


class RecordingSession(BaseSession):
    """Подменяет Telegram API, сохраняя исходящие запросы для проверки."""

    def __init__(self):
        super().__init__()
        self.methods = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, GetMe):
            return User(id=123456789, is_bot=True, first_name="Test", username="TaskTestBot")
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(
                message_id=len(self.methods),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        raise AssertionError(f"Неожиданный запрос: {type(method).__name__}")

    async def stream_content(self, url, **kwargs):
        raise AssertionError("Сетевые скачивания в тестах запрещены")
        yield b""


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "tasks.db"
        self.store = TaskStore(self.path)
        self.store.initialize()

    def test_persistence_order_and_user_isolation(self):
        texts = ['Купить "молоко"; хлеб', "Строка 1\nСтрока 2", "emoji 🚀"]
        ids = [self.store.add_task(101, text) for text in texts]
        self.store.add_task(202, "Чужая задача")
        restarted = TaskStore(self.path)
        restarted.initialize()
        tasks = restarted.list_tasks(101)
        self.assertEqual([task.text for task in tasks], texts)
        self.assertEqual([task.id for task in tasks], ids)
        self.assertTrue(all(task.user == 101 and task.created_at for task in tasks))
        self.assertEqual(len(restarted.list_tasks(202)), 1)
        self.assertEqual(restarted.list_tasks(303), [])

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add_task(101, " \n\t ")
        self.assertEqual(self.store.list_tasks(101), [])

    def test_sql_like_text_is_stored_without_execution(self):
        text = "'); DROP TABLE tasks; --"
        self.store.add_task(101, text)
        self.store.add_task(101, "Следующая задача")
        self.assertEqual(self.store.list_tasks(101)[0].text, text)
        self.assertEqual(len(self.store.list_tasks(101)), 2)


class CsvTests(unittest.TestCase):
    def test_unicode_quotes_separator_and_newline_round_trip(self):
        task = Task(1, 'Проверить "кавычки";\nкириллицу 🚀', 101, "2026-10-08 10:00:00")
        data = build_csv([task])
        self.assertTrue(data.startswith(codecs.BOM_UTF8))
        rows = read_csv(data)
        self.assertEqual(rows, [{
            "id": "1", "text": task.text, "user": "101", "created_at": task.created_at,
            "Статус": "", "Категория": "",
        }])

    def test_empty_export_has_headers(self):
        data = build_csv([]).decode("utf-8-sig")
        self.assertEqual(data, "id;text;user;created_at;Статус;Категория\r\n")

    def test_formula_like_text_is_exported_as_text(self):
        for text in ("=1+1", "+SUM(A1)", "-2+3", "@SUM(A1)", "  =1+1"):
            with self.subTest(text=text):
                rows = read_csv(build_csv([Task(1, text, 101, "2026-10-08")]))
                self.assertEqual(rows[0]["text"], "'" + text)


class SettingsTests(unittest.TestCase):
    @patch("bot.load_dotenv")
    def test_missing_or_invalid_token_has_clear_error(self, load_dotenv):
        for token in ("", "not-a-token"):
            with self.subTest(token=token), patch.dict(os.environ, {"BOT_TOKEN": token}, clear=True):
                with self.assertRaisesRegex(ValueError, "BOT_TOKEN"):
                    load_settings()

    @patch("bot.load_dotenv")
    def test_relative_database_path_is_anchored_to_project(self, load_dotenv):
        token = "123456789:" + "A" * 35
        with patch.dict(os.environ, {"BOT_TOKEN": token, "DB_PATH": "custom/tasks.db"}, clear=True):
            actual_token, path = load_settings()
        self.assertEqual(actual_token, token)
        self.assertEqual(path, PROJECT_DIR / "custom/tasks.db")


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.directory.name) / "tasks.db")
        self.store.initialize()
        self.dispatcher = build_dispatcher(self.store)
        self.session = RecordingSession()
        self.bot = Bot(token="123456789:" + "A" * 35, session=self.session)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.bot.session.close()
        await self.dispatcher.storage.close()
        self.directory.cleanup()

    async def send(self, text, user_id=101, chat_type="private"):
        self.update_id += 1
        self.session.methods.clear()
        update = Update(
            update_id=self.update_id,
            message=Message(
                message_id=self.update_id,
                date=datetime.now(timezone.utc),
                chat=Chat(id=user_id if chat_type == "private" else -100123, type=chat_type),
                from_user=User(id=user_id, is_bot=False, first_name="Test"),
                text=text,
            ),
        )
        await self.dispatcher.feed_update(self.bot, update)
        return self.session.methods

    async def test_start_and_empty_list(self):
        methods = await self.send("/start")
        self.assertIn("Привет", methods[-1].text)
        self.assertIn("/list_csv", methods[-1].text)
        methods = await self.send("/list")
        self.assertIn("Задач пока нет", methods[-1].text)

    async def test_add_requires_text(self):
        for text in ("/add", "/add   "):
            methods = await self.send(text)
            self.assertIn("/add Купить молоко", methods[-1].text)
        self.assertEqual(self.store.list_tasks(101), [])

    async def test_add_and_list_only_own_tasks_in_order(self):
        await self.send("/add Первая задача")
        await self.send("/add Чужая задача", user_id=202)
        await self.send('/add@TaskTestBot Вторая "задача";\nвторая строка')
        # Новые объекты имитируют перезапуск бота с прежним файлом базы.
        self.dispatcher = build_dispatcher(TaskStore(self.store.path))
        methods = await self.send("/list")
        text = methods[-1].text
        self.assertLess(text.index("Первая задача"), text.index('Вторая "задача"'))
        self.assertIn("вторая строка", text)
        self.assertNotIn("Чужая задача", text)

    async def test_csv_sends_document_with_only_own_tasks(self):
        await self.send("/add Моя задача 🚀")
        await self.send("/add Чужая задача", user_id=202)
        methods = await self.send("/list_csv")
        self.assertIsInstance(methods[-1], SendDocument)
        document = methods[-1].document
        self.assertIsInstance(document, BufferedInputFile)
        self.assertEqual(document.filename, "tasks.csv")
        rows = read_csv(document.data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Моя задача 🚀")
        self.assertEqual(rows[0]["user"], "101")

    async def test_empty_csv_is_still_sent(self):
        methods = await self.send("/list_csv")
        self.assertIsInstance(methods[-1], SendDocument)
        self.assertEqual(read_csv(methods[-1].document.data), [])

    async def test_long_list_is_sent_without_losing_text(self):
        tasks = ["🚀" * 1800, "Вторая " + "Я" * 2500, "Третья задача"]
        for text in tasks:
            self.store.add_task(101, text)
        methods = await self.send("/list")
        self.assertGreater(len(methods), 1)
        full_text = "".join(method.text for method in methods)
        for text in tasks:
            self.assertIn(text, full_text)
        for method in methods:
            self.assertLessEqual(len(method.text.encode("utf-16-le")) // 2, 4000)

    async def test_group_command_redirects_without_adding_task(self):
        methods = await self.send("/add Секретная задача", chat_type="group")
        self.assertIn("личный чат", methods[-1].text)
        self.assertEqual(self.store.list_tasks(101), [])

    async def test_unknown_command_shows_help(self):
        methods = await self.send("/unknown")
        self.assertIn("/add", methods[-1].text)

    async def test_database_error_is_reported(self):
        import sqlite3

        with patch.object(self.store, "list_tasks", side_effect=sqlite3.OperationalError("locked")):
            with self.assertLogs("bot", level="ERROR"):
                methods = await self.send("/list")
        self.assertIn("Не удалось", methods[-1].text)

    async def test_unexpected_error_is_not_swallowed(self):
        with patch.object(self.store, "list_tasks", side_effect=RuntimeError("unexpected")):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                await self.send("/list")


class MessageSplitTests(unittest.TestCase):
    def test_emoji_and_cyrillic_survive_splitting(self):
        text = "🚀Я\n" * 3000
        parts = list(split_messages(text))
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(part.encode("utf-16-le")) // 2 <= 4000 for part in parts))


if __name__ == "__main__":
    unittest.main()
