import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, List, Optional

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
    allowed_recruiters = {
        " ".join(name.lower().split()[:2])
        for name in (ctx.settings.friendwork_allowed_recruiters or "").split(",")
        if name.strip()
    }

    def _normalize_name(value: str) -> str:
        return " ".join(value.lower().split()[:2])

    async def notify_admin(text: str) -> None:
        if ctx.settings.admin_chat_id is None:
            logger.info("Admin notification skipped: %s", text)
            return
        try:
            await bot.send_message(ctx.settings.admin_chat_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to notify admin: %s", exc)

    @router.post("/friendwork/webhook")
    async def friendwork_webhook(request: Request) -> dict:
        secret = request.headers.get("x-friendwork-secret")
        auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        expected_auth = f"Bearer {ctx.settings.friendwork_secret}"
        if secret != ctx.settings.friendwork_secret and auth != expected_auth:
            raise HTTPException(status_code=401, detail="Invalid signature")

        raw = await request.json()

        def _get_value(data: dict, *keys: str) -> Any:
            for key in keys:
                if key in data and data[key] is not None:
                    return data[key]
            return None

        job_data = raw.get("job") or raw.get("Job")
        if not job_data and isinstance(raw.get("data"), dict):
            job_data = raw["data"].get("job") or raw["data"].get("Job")

        event_id = _get_value(raw, "event_id", "eventId", "id")
        vacancy_id = _get_value(raw, "vacancy_id", "vacancyId", "jobId", "job_id")
        vacancy_title = _get_value(raw, "vacancy_title", "vacancyTitle") or ""
        recruiter_name = _get_value(raw, "recruiter_name", "recruiterName") or ""
        hiring_manager_ids = _get_value(raw, "hiring_manager_ids", "hiringManagerIds") or []

        status_value = ""
        closed_date = None
        job_url = None
        candidate_count = None
        tech_interview_count = None
        if job_data:
            job_id = _get_value(job_data, "jobId", "id")
            if job_id is not None:
                vacancy_id = str(job_id)
                job_url = f"https://app.friend.work/Job/Edit/{vacancy_id}"
            status = _get_value(job_data, "status", "Status")
            if status:
                status_value = str(status).strip().lower()
                if status_value not in {"closed", "закрыта", "закрыто", "закрыт"}:
                    if ctx.settings.friendwork_api_token and vacancy_id:
                        try:
                            # Give FriendWork a short window to propagate the closed status.
                            await asyncio.sleep(20)
                            live_job = api_client.get_job(str(vacancy_id))
                            live_status = _get_value(live_job, "status", "Status")
                            live_value = str(live_status).strip().lower() if live_status else ""
                            if live_value in {"closed", "закрыта", "закрыто", "закрыт"}:
                                job_data = live_job
                                status_value = live_value
                                job_id = _get_value(job_data, "jobId", "id")
                                if job_id is not None:
                                    vacancy_id = str(job_id)
                                    job_url = f"https://app.friend.work/Job/Edit/{vacancy_id}"
                            else:
                                logger.info(
                                    "Ignoring job status %s for vacancy %s",
                                    status_value,
                                    vacancy_id,
                                )
                                return {"status": "ignored_status", "value": str(status)}
                        except Exception as exc:  # noqa: BLE001
                            logger.error("Failed to verify job status: %s", exc)
                            return {"status": "ignored_status", "value": str(status)}
                    else:
                        logger.info("Ignoring job status %s for vacancy %s", status_value, vacancy_id)
                        return {"status": "ignored_status", "value": str(status)}
            if status_value in {"closed", "закрыта", "закрыто", "закрыт"}:
                tz = ZoneInfo("Europe/Moscow")
                closed_date = datetime.now(tz).strftime("%d.%m.%y")
            vacancy_title = vacancy_title or (_get_value(job_data, "name", "title") or "")
            recruiter_name = recruiter_name or (api_client.extract_recruiter_name(job_data) or "")
            candidate_count = api_client.extract_candidate_count(job_data)
            tech_interview_count = api_client.extract_tech_interview_count(job_data)
            # Prefer counts derived from candidate histories when available.
            try:
                history_count = api_client.count_candidates_in_job(
                    str(vacancy_id),
                    page_size=ctx.settings.candidates_history_page_size,
                    max_pages=50,
                )
                if history_count is not None and history_count > 0:
                    candidate_count = history_count
            except Exception as exc:  # noqa: BLE001
                logger.warning("Candidate count not present in FriendWork job payload for %s: %s", vacancy_id, exc)
            try:
                history_tech = api_client.count_candidates_in_job(
                    str(vacancy_id),
                    status_name=ctx.settings.tech_interview_status_name,
                    status_id=ctx.settings.tech_interview_status_id,
                    page_size=ctx.settings.candidates_history_page_size,
                    max_pages=50,
                )
                if history_tech is not None and history_tech > 0:
                    tech_interview_count = history_tech
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tech interview count not present in FriendWork job payload for %s: %s", vacancy_id, exc)
            hiring_manager_ids = []
            if ctx.sheets:
                names = api_client.extract_hiring_manager_names(job_data)
                if recruiter_name:
                    names = [
                        name for name in names if _normalize_name(name) != _normalize_name(recruiter_name)
                    ]
                if names:
                    logger.info("FriendWork hiring manager names: %s", ", ".join(names))
                for name in names:
                    user = ctx.sheets.get_user_by_name(name)
                    if user and user.telegram_id:
                        logger.info("Matched hiring manager '%s' to Telegram ID %s via sheet lookup.", name, user.telegram_id)
                        hiring_manager_ids.append(user.telegram_id)
                        continue
                    if user and user.username:
                        stored = await ctx.user_store.find_by_username(user.username)
                        if stored:
                            logger.info(
                                "Matched hiring manager '%s' to Telegram ID %s via username '%s'.",
                                name,
                                stored.telegram_id,
                                user.username,
                            )
                            hiring_manager_ids.append(stored.telegram_id)
                            continue
                    stored = await ctx.user_store.find_by_full_name(name)
                    if stored:
                        logger.info("Matched hiring manager '%s' to Telegram ID %s via full name cache.", name, stored.telegram_id)
                        hiring_manager_ids.append(stored.telegram_id)
                        continue
                    if user:
                        logger.warning(
                            "User found for '%s' but missing Telegram ID. Sheet user: full_name='%s', username='%s', telegram_id='%s'",
                            name,
                            user.full_name,
                            user.username,
                            user.telegram_id,
                        )
                    else:
                        logger.warning("No sheet user match found for hiring manager '%s'.", name)
                if names and not hiring_manager_ids:
                    logger.warning("No matching users found for hiring manager names.")

        if not vacancy_id:
            await notify_admin("FriendWork webhook missing vacancy id.")
            return {"status": "missing_vacancy_id"}

        if status_value in {"closed", "закрыта", "закрыто", "закрыт"}:
            # Deduplicate repeated close events for the same vacancy.
            event_id = f"closed-{vacancy_id}"
        event_id = str(event_id or f"job-{vacancy_id}-{datetime.utcnow().isoformat()}")
        if await event_store.seen(event_id):
            return {"status": "duplicate"}
        await event_store.mark(event_id)

        # If hiring manager IDs or recruiter name are not provided, fetch job data.
        if (not hiring_manager_ids or not recruiter_name or not vacancy_title) and ctx.settings.friendwork_api_token:
            try:
                job_data = api_client.get_job(str(vacancy_id))
                vacancy_title = vacancy_title or job_data.get("name") or ""
                recruiter_name = recruiter_name or (api_client.extract_recruiter_name(job_data) or "")
                job_url = job_url or f"https://app.friend.work/Job/Edit/{vacancy_id}"
                if not closed_date:
                    tz = ZoneInfo("Europe/Moscow")
                    closed_date = datetime.now(tz).strftime("%d.%m.%y")
                if candidate_count is None:
                    candidate_count = api_client.extract_candidate_count(job_data)
                if tech_interview_count is None:
                    tech_interview_count = api_client.extract_tech_interview_count(job_data)
                try:
                    history_count = api_client.count_candidates_in_job(
                        str(vacancy_id),
                        page_size=ctx.settings.candidates_history_page_size,
                    )
                    if history_count is not None and history_count > 0:
                        candidate_count = history_count
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Candidate count not present in FriendWork job payload for %s: %s", vacancy_id, exc)
                try:
                    history_tech = api_client.count_candidates_in_job(
                        str(vacancy_id),
                        status_name=ctx.settings.tech_interview_status_name,
                        status_id=ctx.settings.tech_interview_status_id,
                        page_size=ctx.settings.candidates_history_page_size,
                    )
                    if history_tech is not None and history_tech > 0:
                        tech_interview_count = history_tech
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Tech interview count not present in FriendWork job payload for %s: %s", vacancy_id, exc)
                if not hiring_manager_ids and ctx.sheets:
                    names = api_client.extract_hiring_manager_names(job_data)
                    if names:
                        logger.info("FriendWork hiring manager names: %s", ", ".join(names))
                    for name in names:
                        user = ctx.sheets.get_user_by_name(name)
                        if user and user.telegram_id:
                            logger.info("Matched hiring manager '%s' to Telegram ID %s via sheet lookup.", name, user.telegram_id)
                            hiring_manager_ids.append(user.telegram_id)
                            continue
                        if user and user.username:
                            stored = await ctx.user_store.find_by_username(user.username)
                            if stored:
                                logger.info(
                                    "Matched hiring manager '%s' to Telegram ID %s via username '%s'.",
                                    name,
                                    stored.telegram_id,
                                    user.username,
                                )
                                hiring_manager_ids.append(stored.telegram_id)
                                continue
                        stored = await ctx.user_store.find_by_full_name(name)
                        if stored:
                            logger.info("Matched hiring manager '%s' to Telegram ID %s via full name cache.", name, stored.telegram_id)
                            hiring_manager_ids.append(stored.telegram_id)
                            continue
                        if user:
                            logger.warning(
                                "User found for '%s' but missing Telegram ID. Sheet user: full_name='%s', username='%s', telegram_id='%s'",
                                name,
                                user.full_name,
                                user.username,
                                user.telegram_id,
                            )
                        else:
                            logger.warning("No sheet user match found for hiring manager '%s'.", name)
                    if names and not hiring_manager_ids:
                        logger.warning("No matching users found for hiring manager names.")
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to fetch job data from FriendWork: %s", exc)

        if allowed_recruiters and recruiter_name:
            if _normalize_name(recruiter_name) not in allowed_recruiters:
                return {"status": "skipped_not_allowed"}

        vacancy = VacancyAssignment(
            vacancy_id=str(vacancy_id),
            vacancy_title=vacancy_title,
            recruiter_name=recruiter_name,
            hiring_manager_ids=[int(x) for x in hiring_manager_ids if str(x).isdigit()],
            closed_date=closed_date,
            job_url=job_url,
            candidate_count=candidate_count,
            tech_interview_count=tech_interview_count,
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
