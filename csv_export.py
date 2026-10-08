"""Создание CSV в памяти для отправки пользователю в Telegram."""

import csv
import io
from collections.abc import Iterable

from storage import Task


def build_csv(tasks: Iterable[Task]) -> bytes:
    """Экспортирует задачи в UTF-8 с BOM и двумя колонками для эксперимента."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(["id", "text", "user", "created_at", "Статус", "Категория"])
    for task in tasks:
        text = task.text
        # Excel должен воспринимать пользовательский текст как текст, не формулу.
        if text.lstrip().startswith(("=", "+", "-", "@")):
            text = "'" + text
        writer.writerow([task.id, text, task.user, task.created_at, "", ""])
    # BOM помогает Excel распознать кириллицу; csv.writer экранирует переносы.
    return output.getvalue().encode("utf-8-sig")
