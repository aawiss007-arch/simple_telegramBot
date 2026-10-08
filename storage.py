"""Постоянное хранение задач с помощью встроенного модуля sqlite3."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Task:
    """Одна задача; user содержит числовой Telegram ID владельца."""

    id: int
    text: str
    user: int
    created_at: str


class TaskStore:
    """Работает с файлом SQLite, открывая отдельное соединение для операции."""

    def __init__(self, path: Path) -> None:
        """Запоминает путь к базе; соединение между вызовами не хранится."""
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        """Открывает соединение с именованными полями и ожиданием блокировки."""
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        """Создаёт папку, таблицу и индекс, сохраняя уже существующие задачи."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    "user" INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS tasks_user_id ON tasks ("user", id)'
            )

    def add_task(self, user_id: int, text: str) -> int:
        """Сохраняет непустой текст и возвращает ID новой задачи."""
        text = text.strip()
        if not text:
            raise ValueError("Текст задачи не должен быть пустым.")
        with closing(self._connect()) as connection, connection:
            # Параметры SQL сохраняют кавычки и произвольный текст как данные.
            cursor = connection.execute(
                'INSERT INTO tasks (text, "user") VALUES (?, ?)',
                (text, user_id),
            )
            return cursor.lastrowid

    def list_tasks(self, user_id: int) -> list[Task]:
        """Возвращает только задачи владельца в порядке добавления."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT id, text, "user", created_at FROM tasks '
                'WHERE "user" = ? ORDER BY id ASC',
                (user_id,),
            ).fetchall()
        return [Task(**dict(row)) for row in rows]
