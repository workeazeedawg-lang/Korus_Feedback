from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from .models import FeedbackRecord, User, VacancyAssignment


class UserStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._users: Dict[int, User] = {}

    async def get(self, telegram_id: int) -> Optional[User]:
        async with self._lock:
            return self._users.get(telegram_id)

    async def upsert(self, user: User) -> None:
        async with self._lock:
            self._users[user.telegram_id] = user

    async def bulk_upsert(self, users: Iterable[User]) -> None:
        async with self._lock:
            for user in users:
                self._users[user.telegram_id] = user

    async def find_by_username(self, username: str) -> Optional[User]:
        if not username:
            return None
        needle = username.strip().lower()
        async with self._lock:
            for user in self._users.values():
                if user.username and user.username.strip().lower() == needle:
                    return user
        return None

    async def find_by_full_name(self, full_name: str) -> Optional[User]:
        if not full_name:
            return None

        def normalize(value: str) -> str:
            parts = value.strip().lower().split()
            return " ".join(parts[:2])

        needle = normalize(full_name)
        swapped = " ".join(reversed(full_name.strip().lower().split()[:2]))
        async with self._lock:
            for user in self._users.values():
                candidate = normalize(user.full_name)
                if candidate == needle or candidate == swapped:
                    return user
        return None


class VacancyStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._vacancies: Dict[str, VacancyAssignment] = {}

    async def upsert(self, vacancy: VacancyAssignment) -> None:
        async with self._lock:
            self._vacancies[vacancy.vacancy_id] = vacancy

    async def get(self, vacancy_id: str) -> Optional[VacancyAssignment]:
        async with self._lock:
            return self._vacancies.get(vacancy_id)


class EventStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._seen: set[str] = set()

    async def seen(self, event_id: str) -> bool:
        async with self._lock:
            return event_id in self._seen

    async def mark(self, event_id: str) -> None:
        async with self._lock:
            self._seen.add(event_id)


class FeedbackBuffer:
    """Keeps recent feedback for debugging or fallback storage."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: List[FeedbackRecord] = []

    async def add(self, record: FeedbackRecord) -> None:
        async with self._lock:
            self._items.append(record)

    async def list_recent(self, limit: int = 20) -> List[FeedbackRecord]:
        async with self._lock:
            return list(self._items[-limit:])


@dataclass
class Reminder:
    telegram_id: int
    vacancy_id: str
    next_at: datetime
    first_at: datetime
    remind_count: int = 0
    notified_admin: bool = False


class ReminderStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: Dict[Tuple[int, str], Reminder] = {}

    async def get(self, telegram_id: int, vacancy_id: str) -> Optional[Reminder]:
        async with self._lock:
            return self._items.get((telegram_id, vacancy_id))

    async def upsert(self, reminder: Reminder) -> None:
        async with self._lock:
            self._items[(reminder.telegram_id, reminder.vacancy_id)] = reminder

    async def remove(self, telegram_id: int, vacancy_id: str) -> None:
        async with self._lock:
            self._items.pop((telegram_id, vacancy_id), None)

    async def due(self, now: datetime) -> List[Reminder]:
        async with self._lock:
            return [item for item in self._items.values() if item.next_at <= now]
