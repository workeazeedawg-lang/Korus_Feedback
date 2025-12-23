import logging
from typing import List, Optional

from aiogram import Bot, Dispatcher
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .bot import AppContext, send_feedback_request
from .models import VacancyAssignment
from .storage import EventStore

logger = logging.getLogger(__name__)


class FriendWorkEvent(BaseModel):
    event_id: str
    vacancy_id: str
    vacancy_title: str
    recruiter_name: str
    hiring_manager_ids: List[int]


def create_friendwork_router(
    ctx: AppContext, bot: Bot, dp: Dispatcher, event_store: EventStore
) -> APIRouter:
    router = APIRouter()

    async def notify_admin(text: str) -> None:
        if ctx.settings.admin_chat_id is None:
            logger.info("Admin notification skipped: %s", text)
            return
        try:
            await bot.send_message(ctx.settings.admin_chat_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to notify admin: %s", exc)

    @router.post("/friendwork/webhook")
    async def friendwork_webhook(payload: FriendWorkEvent, request: Request) -> dict:
        secret = request.headers.get("x-friendwork-secret")
        if secret != ctx.settings.friendwork_secret:
            raise HTTPException(status_code=401, detail="Invalid signature")

        if await event_store.seen(payload.event_id):
            return {"status": "duplicate"}
        await event_store.mark(payload.event_id)

        vacancy = VacancyAssignment(
            vacancy_id=payload.vacancy_id,
            vacancy_title=payload.vacancy_title,
            recruiter_name=payload.recruiter_name,
            hiring_manager_ids=payload.hiring_manager_ids,
        )
        await ctx.vacancy_store.upsert(vacancy)

        if not vacancy.hiring_manager_ids:
            await notify_admin(f"No hiring managers found for vacancy {vacancy.vacancy_id}")
            return {"status": "no_managers"}

        await send_feedback_request(bot, ctx, vacancy)
        return {"status": "ok"}

    return router
