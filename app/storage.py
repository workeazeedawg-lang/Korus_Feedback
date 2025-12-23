from __future__ import annotations

import asyncio
from typing import Dict, Iterable, List, Optional

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
