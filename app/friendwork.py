import logging
from typing import List, Optional

from aiogram import Bot, Dispatcher
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .bot import AppContext, send_feedback_request
from .friendwork_api import FriendWorkClient
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
    api_client = FriendWorkClient(ctx.settings.friendwork_api_base, ctx.settings.friendwork_api_token)

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

        vacancy_title = payload.vacancy_title
        recruiter_name = payload.recruiter_name
        hiring_manager_ids = payload.hiring_manager_ids

        # If hiring manager IDs or recruiter name are not provided, fetch job data.
        if (not hiring_manager_ids or not recruiter_name or not vacancy_title) and ctx.settings.friendwork_api_token:
            try:
                job_data = api_client.get_job(payload.vacancy_id)
                vacancy_title = vacancy_title or job_data.get("name") or ""
                recruiter_name = recruiter_name or (api_client.extract_recruiter_name(job_data) or "")
                if not hiring_manager_ids and ctx.sheets:
                    names = api_client.extract_hiring_manager_names(job_data)
                    for name in names:
                        user = ctx.sheets.get_user_by_name(name)
                        if user and user.telegram_id:
                            hiring_manager_ids.append(user.telegram_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to fetch job data from FriendWork: %s", exc)

        vacancy = VacancyAssignment(
            vacancy_id=payload.vacancy_id,
            vacancy_title=vacancy_title,
            recruiter_name=recruiter_name,
            hiring_manager_ids=hiring_manager_ids,
        )
        await ctx.vacancy_store.upsert(vacancy)
        if ctx.sheets:
            try:
                ctx.sheets.upsert_vacancy(vacancy)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to store vacancy in sheet: %s", exc)

        if not vacancy.hiring_manager_ids:
            await notify_admin(f"No hiring managers found for vacancy {vacancy.vacancy_id}")
            return {"status": "no_managers"}

        await send_feedback_request(bot, ctx, vacancy)
        return {"status": "ok"}

    return router
