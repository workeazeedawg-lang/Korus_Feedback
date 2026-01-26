from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .config import Settings
from .models import FeedbackRecord, User, VacancyAssignment
from .sheets import GoogleSheetClient
from .speech import SpeechToText
from .storage import FeedbackBuffer, Reminder, ReminderStore, UserStore, VacancyStore

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    user_store: UserStore
    vacancy_store: VacancyStore
    feedback_buffer: FeedbackBuffer
    reminders: ReminderStore
    sheets: GoogleSheetClient | None
    speech: SpeechToText | None


class RegistrationStates(StatesGroup):
    waiting_full_name = State()
    waiting_title = State()
    waiting_contact = State()


class FeedbackStates(StatesGroup):
    overall_rating = State()
    recruiter = State()
    comms_rating = State()
    timeliness_rating = State()
    relevance_rating = State()
    process_quality_rating = State()
    recommendations = State()
    confirm = State()


def feedback_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="РћСЃС‚Р°РІРёС‚СЊ РѕС‚Р·С‹РІ СЃРµР№С‡Р°СЃ", callback_data=f"start_feedback:{vacancy_id}")],
            [InlineKeyboardButton(text="РќР°РїРѕРјРЅРёС‚СЊ РїРѕР·Р¶Рµ", callback_data=f"remind_feedback:{vacancy_id}")],
        ]
    )


def recruiter_choice_keyboard(recruiter_name: str) -> InlineKeyboardMarkup:
    label = recruiter_name.strip() if recruiter_name.strip() else "РќРµ Р·РЅР°СЋ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"РСЃРїРѕР»СЊР·РѕРІР°С‚СЊ: {label}", callback_data="recruiter_use_default")],
            [InlineKeyboardButton(text="Р’РІРµСЃС‚Рё РґСЂСѓРіРѕРµ РёРјСЏ", callback_data="recruiter_other")],
        ]
    )


def confirm_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Р”Р°, СЃРѕС…СЂР°РЅРёС‚СЊ", callback_data="confirm_feedback_yes")],
            [InlineKeyboardButton(text="РќРµС‚, РѕС‚РјРµРЅРёС‚СЊ", callback_data="confirm_feedback_no")],
        ]
    )



def timeliness_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="timeliness_yes")],
            [InlineKeyboardButton(text="Нет", callback_data="timeliness_no")],
        ]
    )


def next_daily_moscow(hour: int = 10) -> datetime:
    tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    target = datetime.combine(now.date(), time(hour=hour, minute=0, tzinfo=tz))
    if now >= target:
        target = target + timedelta(days=1)
    return target


async def send_feedback_request(bot: Bot, ctx: AppContext, vacancy: VacancyAssignment) -> None:
    for manager_id in vacancy.hiring_manager_ids:
        user = await ctx.user_store.get(manager_id)
        if user and user.status != "active":
            continue
        text = (
            f"Р’Р°РєР°РЅСЃРёСЏ Р·Р°РєСЂС‹С‚Р°: {vacancy.vacancy_title}\n"
            f"Р РµРєСЂСѓС‚РµСЂ: {vacancy.recruiter_name}\n"
            "РњРѕР¶РµС‚Рµ РѕСЃС‚Р°РІРёС‚СЊ РѕС‚Р·С‹РІ СЃРµР№С‡Р°СЃ?"
        )
        try:
            await bot.send_message(manager_id, text, reply_markup=feedback_keyboard(vacancy.vacancy_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send feedback request to %s: %s", manager_id, exc)
        else:
            # Track initial request to notify admin if no action in 5 days.
            existing = await ctx.reminders.get(manager_id, vacancy.vacancy_id)
            if not existing:
                now = datetime.now(ZoneInfo("Europe/Moscow"))
                next_at = now + timedelta(days=5)
                await ctx.reminders.upsert(
                    Reminder(
                        manager_id,
                        vacancy.vacancy_id,
                        next_at,
                        first_at=now,
                        remind_count=0,
                        notified_admin=False,
                    )
                )


async def send_feedback_request_to_user(
    bot: Bot, ctx: AppContext, vacancy: VacancyAssignment, manager_id: int
) -> None:
    user = await ctx.user_store.get(manager_id)
    if user and user.status != "active":
        return
    text = (
        f"Р’Р°РєР°РЅСЃРёСЏ Р·Р°РєСЂС‹С‚Р°: {vacancy.vacancy_title}\n"
        f"Р РµРєСЂСѓС‚РµСЂ: {vacancy.recruiter_name}\n"
        "РњРѕР¶РµС‚Рµ РѕСЃС‚Р°РІРёС‚СЊ РѕС‚Р·С‹РІ СЃРµР№С‡Р°СЃ?"
    )
    try:
        await bot.send_message(manager_id, text, reply_markup=feedback_keyboard(vacancy.vacancy_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to send feedback request to %s: %s", manager_id, exc)


def register_handlers(router: Router, ctx: AppContext) -> None:
    async def get_registered_user(telegram_id: int, username: str | None) -> User | None:
        user = await ctx.user_store.get(telegram_id)
        if user:
            return user
        if ctx.sheets:
            try:
                user = ctx.sheets.get_user(telegram_id, username)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to fetch user from sheet: %s", exc)
                return None
            if user:
                await ctx.user_store.upsert(user)
        return user

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer("Р—РґСЂР°РІСЃС‚РІСѓР№С‚Рµ! РСЃРїРѕР»СЊР·СѓР№С‚Рµ /register, С‡С‚РѕР±С‹ РїРѕРґС‚РІРµСЂРґРёС‚СЊ РґРѕСЃС‚СѓРї, РёР»Рё Р¶РґРёС‚Рµ Р·Р°РїСЂРѕСЃ РЅР° РѕС‚Р·С‹РІ.")

    @router.message(Command("register"))
    async def register(message: Message, state: FSMContext) -> None:
        existing = await get_registered_user(message.from_user.id, message.from_user.username)
        if existing:
            await message.answer("Р’С‹ СѓР¶Рµ Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅС‹. РЎРїР°СЃРёР±Рѕ!")
            return
        await state.set_state(RegistrationStates.waiting_full_name)
        await message.answer("РЈРєР°Р¶РёС‚Рµ РІР°С€Рµ Р¤РРћ РґР»СЏ Р·Р°РІРµСЂС€РµРЅРёСЏ СЂРµРіРёСЃС‚СЂР°С†РёРё.")

    @router.message(RegistrationStates.waiting_full_name)
    async def save_full_name(message: Message, state: FSMContext) -> None:
        await state.update_data(full_name=message.text.strip())
        await state.set_state(RegistrationStates.waiting_title)
        await message.answer("Р’Р°С€Р° РґРѕР»Р¶РЅРѕСЃС‚СЊ?")

    @router.message(RegistrationStates.waiting_title)
    async def save_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text.strip())
        await state.set_state(RegistrationStates.waiting_contact)
        await message.answer("РљРѕРЅС‚Р°РєС‚ (email/С‚РµР»РµС„РѕРЅ)?")

    @router.message(RegistrationStates.waiting_contact)
    async def finish_registration(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        user = User(
            telegram_id=message.from_user.id,
            full_name=data.get("full_name", message.from_user.full_name or ""),
            username=message.from_user.username or str(message.from_user.id),
            title=data.get("title"),
            contact=message.text.strip(),
            permission_level="hiring_manager",
        )
        await ctx.user_store.upsert(user)
        if ctx.sheets:
            try:
                ctx.sheets.upsert_user(user)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to store user in sheet: %s", exc)
        await state.clear()
        await message.answer("Р’С‹ СѓСЃРїРµС€РЅРѕ Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅС‹. РЎРїР°СЃРёР±Рѕ!")

    @router.callback_query(lambda c: c.data and c.data.startswith("start_feedback:"))
    async def handle_start_feedback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        vacancy_id = callback.data.split(":", maxsplit=1)[1]
        await ctx.reminders.remove(callback.from_user.id, vacancy_id)
        vacancy = await ctx.vacancy_store.get(vacancy_id)
        if not vacancy and ctx.sheets:
            try:
                vacancy = ctx.sheets.get_vacancy(vacancy_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to fetch vacancy from sheet: %s", exc)
                vacancy = None
            if vacancy:
                await ctx.vacancy_store.upsert(vacancy)
        if not vacancy:
            await callback.message.answer("РќРµ РјРѕРіСѓ РЅР°Р№С‚Рё РІР°РєР°РЅСЃРёСЋ. РќР°РїРёС€РёС‚Рµ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂСѓ.")
            return
        user = await get_registered_user(callback.from_user.id, callback.from_user.username)
        if not user:
            await callback.message.answer(
                f"Р’С‹ РЅРµ Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅС‹. РќР°РїРёС€РёС‚Рµ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂСѓ ({ctx.settings.admin_contact})."
            )
            return
        await state.update_data(
            vacancy_id=vacancy.vacancy_id,
            vacancy_title=vacancy.vacancy_title,
            recruiter_name=vacancy.recruiter_name,
            hiring_manager_full_name=user.full_name,
            closed_date=vacancy.closed_date or "",
            job_url=vacancy.job_url or "",
            candidate_count=vacancy.candidate_count or 0,
            tech_interview_count=vacancy.tech_interview_count or 0,
        )
        await state.set_state(FeedbackStates.overall_rating)
        await callback.message.answer("РћР±С‰Р°СЏ РѕС†РµРЅРєР° СЂР°Р±РѕС‚С‹ СЂРµРєСЂСѓС‚РµСЂР° (1-5)?")

    @router.callback_query(lambda c: c.data and c.data.startswith("remind_feedback:"))
    async def handle_remind_feedback(callback: CallbackQuery) -> None:
        await callback.answer()
        vacancy_id = callback.data.split(":", maxsplit=1)[1]
        remind_at = next_daily_moscow()
        now = datetime.now(ZoneInfo("Europe/Moscow"))
        existing = await ctx.reminders.get(callback.from_user.id, vacancy_id)
        first_at = existing.first_at if existing else now
        remind_count = (existing.remind_count + 1) if existing else 1
        notified_admin = existing.notified_admin if existing else False
        if remind_count >= 5 and not notified_admin and ctx.settings.admin_chat_id is not None:
            vacancy = await ctx.vacancy_store.get(vacancy_id)
            if not vacancy and ctx.sheets:
                try:
                    vacancy = ctx.sheets.get_vacancy(vacancy_id)
                except Exception:
                    vacancy = None
            link = vacancy.job_url if vacancy and vacancy.job_url else f"https://app.friend.work/Job/Edit/{vacancy_id}"
            text = (
                f"Нанимающий менеджер по вакансии \"{link}\" "
                "не прошёл опрос по работе рекрутера в срок."
            )
            try:
                await callback.bot.send_message(ctx.settings.admin_chat_id, text)
                notified_admin = True
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to notify admin: %s", exc)
        await ctx.reminders.upsert(
            Reminder(
                callback.from_user.id,
                vacancy_id,
                remind_at,
                first_at=first_at,
                remind_count=remind_count,
                notified_admin=notified_admin,
            )
        )
        await callback.message.answer(
            f"РҐРѕСЂРѕС€Рѕ, РЅР°РїРѕРјРЅСЋ {remind_at.strftime('%d.%m.%Y РІ %H:%M')} РњРЎРљ."
        )
    @router.message(Command("feedback"))
    async def manual_feedback(message: Message, state: FSMContext) -> None:
        vacancy = VacancyAssignment(
            vacancy_id="manual",
            vacancy_title="Manual trigger",
            recruiter_name="Unknown",
            hiring_manager_ids=[message.from_user.id],
            closed_date="",
            job_url="",
            candidate_count=0,
            tech_interview_count=0,
        )
        user = await get_registered_user(message.from_user.id, message.from_user.username)
        if not user:
            await message.answer(
                f"Р’С‹ РЅРµ Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅС‹. РќР°РїРёС€РёС‚Рµ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂСѓ ({ctx.settings.admin_contact})."
            )
            return
        await ctx.vacancy_store.upsert(vacancy)
        await state.update_data(
            vacancy_id=vacancy.vacancy_id,
            vacancy_title=vacancy.vacancy_title,
            recruiter_name=vacancy.recruiter_name,
            hiring_manager_full_name=user.full_name,
            closed_date=vacancy.closed_date or "",
            job_url=vacancy.job_url or "",
            candidate_count=vacancy.candidate_count or 0,
            tech_interview_count=vacancy.tech_interview_count or 0,
        )
        await state.set_state(FeedbackStates.overall_rating)
        await message.answer("Р—Р°РїСѓСЃРєР°СЋ СЂСѓС‡РЅРѕР№ РѕРїСЂРѕСЃ. РћР±С‰Р°СЏ РѕС†РµРЅРєР° СЂР°Р±РѕС‚С‹ СЂРµРєСЂСѓС‚РµСЂР° (1-5)?")

    async def _validate_rating(message: Message, min_value: int = 1, max_value: int = 5) -> int | None:
        try:
            value = int(message.text.strip())
            if min_value <= value <= max_value:
                return value
        except Exception:
            pass
        await message.answer(f"Р’РІРµРґРёС‚Рµ С‡РёСЃР»Рѕ РѕС‚ {min_value} РґРѕ {max_value}.")
        return None

    @router.message(FeedbackStates.overall_rating)
    async def receive_overall(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(overall_rating=rating)
        data = await state.get_data()
        await state.set_state(FeedbackStates.recruiter)
        await message.answer(
            "РЎ РєР°РєРёРј СЂРµРєСЂСѓС‚РµСЂРѕРј РІС‹ СЂР°Р±РѕС‚Р°Р»Рё?",
            reply_markup=recruiter_choice_keyboard(data.get("recruiter_name", "")),
        )

    @router.callback_query(lambda c: c.data == "recruiter_use_default")
    async def recruiter_use_default(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        data = await state.get_data()
        recruiter_name = data.get("recruiter_name", "РќРµРёР·РІРµСЃС‚РµРЅ")
        await state.update_data(recruiter_name=recruiter_name)
        await state.set_state(FeedbackStates.comms_rating)
        await callback.message.answer("РљР°Рє РѕС†РµРЅРёРІР°РµС‚Рµ РєРѕРјРјСѓРЅРёРєР°С†РёСЋ СЃ СЂРµРєСЂСѓС‚РµСЂРѕРј? (1-5)")

    @router.callback_query(lambda c: c.data == "recruiter_other")
    async def recruiter_other(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.set_state(FeedbackStates.recruiter)
        await callback.message.answer("Р’РІРµРґРёС‚Рµ РёРјСЏ СЂРµРєСЂСѓС‚РµСЂР°.")

    @router.message(FeedbackStates.recruiter)
    async def receive_recruiter(message: Message, state: FSMContext) -> None:
        recruiter_name = message.text.strip()
        await state.update_data(recruiter_name=recruiter_name)
        await state.set_state(FeedbackStates.comms_rating)
        await message.answer("РљР°Рє РѕС†РµРЅРёРІР°РµС‚Рµ РєРѕРјРјСѓРЅРёРєР°С†РёСЋ СЃ СЂРµРєСЂСѓС‚РµСЂРѕРј? (1-5)")

    @router.message(FeedbackStates.comms_rating)
    async def receive_comms(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(comms_rating=rating)
        await state.set_state(FeedbackStates.timeliness_rating)
        await message.answer("Вакансия закрыта в комфортные сроки? Да/Нет", reply_markup=timeliness_keyboard())

    @router.callback_query(lambda c: c.data in {"timeliness_yes", "timeliness_no"})
    async def receive_time_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        value = "да" if callback.data == "timeliness_yes" else "нет"
        await state.update_data(timeliness_rating=value)
        await state.set_state(FeedbackStates.relevance_rating)
        await callback.message.answer("РќР°СЃРєРѕР»СЊРєРѕ СЂРµР»РµРІР°РЅС‚РЅС‹ РєР°РЅРґРёРґР°С‚С‹? (1-5)")

    @router.message(FeedbackStates.timeliness_rating)
    async def receive_time_text(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip().lower()
        if value not in {"да", "нет", "yes", "no"}:
            await message.answer("Ответьте 'Да' или 'Нет'.")
            return
        normalized = "да" if value in {"да", "yes"} else "нет"
        await state.update_data(timeliness_rating=normalized)
        await state.set_state(FeedbackStates.relevance_rating)
        await message.answer("РќР°СЃРєРѕР»СЊРєРѕ СЂРµР»РµРІР°РЅС‚РЅС‹ РєР°РЅРґРёРґР°С‚С‹? (1-5)")
    @router.message(FeedbackStates.relevance_rating)
    async def receive_relevance(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(relevance_rating=rating)
        await state.set_state(FeedbackStates.process_quality_rating)
        await message.answer("РљР°Рє РѕС†РµРЅРёРІР°РµС‚Рµ РєР°С‡РµСЃС‚РІРѕ РїСЂРѕС†РµСЃСЃР° (РѕС€РёР±РєРё, С„РёРґР±РµРє, РїРѕРґРґРµСЂР¶РєР°, HR-РёРЅС‚РµСЂРІСЊСЋ)? (1-5)")

    @router.message(FeedbackStates.process_quality_rating)
    async def receive_process_quality(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(process_quality_rating=rating)
        await state.set_state(FeedbackStates.recommendations)
        await message.answer("РљР°РєРёРµ СЂРµРєРѕРјРµРЅРґР°С†РёРё РїРѕ СѓР»СѓС‡С€РµРЅРёСЋ СЂР°Р±РѕС‚С‹ СЂРµРєСЂСѓС‚РµСЂР°? РћС‚РїСЂР°РІСЊС‚Рµ С‚РµРєСЃС‚ РёР»Рё РіРѕР»РѕСЃ.")

    @router.message(FeedbackStates.recommendations)
    async def receive_recommendations(message: Message, state: FSMContext, bot: Bot) -> None:
        text: str | None = None
        if message.voice:
            if ctx.speech is None:
                await message.answer("Р Р°СЃРїРѕР·РЅР°РІР°РЅРёРµ СЂРµС‡Рё РЅРµ РЅР°СЃС‚СЂРѕРµРЅРѕ. РћС‚РїСЂР°РІСЊС‚Рµ С‚РµРєСЃС‚РѕРј, РїРѕР¶Р°Р»СѓР№СЃС‚Р°.")
                return
            await message.answer("РџСЂРµРѕР±СЂР°Р·СѓРµРј РіРѕР»РѕСЃРѕРІРѕРµ РІ С‚РµРєСЃС‚...")
            file = await bot.get_file(message.voice.file_id)
            buffer = await bot.download_file(file.file_path)
            transcription = await ctx.speech.transcribe_bytes(buffer.read())
            if not transcription:
                await message.answer("РќРµ СѓРґР°Р»РѕСЃСЊ СЂР°СЃРїРѕР·РЅР°С‚СЊ РіРѕР»РѕСЃ. РћС‚РїСЂР°РІСЊС‚Рµ С‚РµРєСЃС‚РѕРј, РїРѕР¶Р°Р»СѓР№СЃС‚Р°.")
                return
            text = transcription
            await message.answer(f"РўСЂР°РЅСЃРєСЂРёРїС†РёСЏ:\n{text}")
        elif message.text:
            text = message.text.strip()

        if not text:
            await message.answer("РћС‚РїСЂР°РІСЊС‚Рµ С‚РµРєСЃС‚ РёР»Рё РіРѕР»РѕСЃРѕРІРѕРµ СЃ СЂРµРєРѕРјРµРЅРґР°С†РёСЏРјРё.")
            return

        await state.update_data(recommendations=text, feedback_comment=text)
        data = await state.get_data()
        await state.set_state(FeedbackStates.confirm)
        summary = (
            f"Р’Р°РєР°РЅСЃРёСЏ: {data.get('vacancy_title')}\n"
            f"Р РµРєСЂСѓС‚РµСЂ: {data.get('recruiter_name')}\n"
            f"РћР±С‰Р°СЏ РѕС†РµРЅРєР°: {data.get('overall_rating')}\n"
            f"РљРѕРјРјСѓРЅРёРєР°С†РёСЏ: {data.get('comms_rating')}\n"
            f"РЎСЂРѕРєРё Р·Р°РєСЂС‹С‚РёСЏ: {data.get('timeliness_rating')}\n"
            f"Р РµР»РµРІР°РЅС‚РЅРѕСЃС‚СЊ РєР°РЅРґРёРґР°С‚РѕРІ: {data.get('relevance_rating')}\n"
            f"РљР°С‡РµСЃС‚РІРѕ РїСЂРѕС†РµСЃСЃР°: {data.get('process_quality_rating')}\n"
            f"Р РµРєРѕРјРµРЅРґР°С†РёРё: {text}\n\n"
            "РЎРѕС…СЂР°РЅРёС‚СЊ РѕС‚Р·С‹РІ? РћС‚РІРµС‚СЊС‚Рµ 'РґР°' РґР»СЏ СЃРѕС…СЂР°РЅРµРЅРёСЏ РёР»Рё 'РЅРµС‚' РґР»СЏ РѕС‚РјРµРЅС‹."
        )
        await message.answer(summary, reply_markup=confirm_feedback_keyboard())

    async def _finalize_feedback(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        vacancy_id = data.get("vacancy_id", "")
        await ctx.reminders.remove(message.from_user.id, vacancy_id)
        record = FeedbackRecord(
            vacancy_id=data.get("vacancy_id", ""),
            vacancy_title=data.get("vacancy_title", ""),
            recruiter_name=data.get("recruiter_name", ""),
            hiring_manager_full_name=data.get("hiring_manager_full_name", message.from_user.full_name or ""),
            closed_date=data.get("closed_date", ""),
            job_url=data.get("job_url", ""),
            candidate_count=int(data.get("candidate_count") or 0),
            tech_interview_count=int(data.get("tech_interview_count") or 0),
            telegram_user_id=message.from_user.id,
            feedback_comment=data.get("feedback_comment", ""),
            overall_rating=data.get("overall_rating", 0),
            comms_rating=data.get("comms_rating", 0),
            timeliness_rating=str(data.get("timeliness_rating") or ""),
            relevance_rating=data.get("relevance_rating", 0),
            process_quality_rating=data.get("process_quality_rating", 0),
            recommendations=data.get("recommendations", ""),
            submitted_at=datetime.utcnow(),
        )
        if ctx.sheets:
            try:
                ctx.sheets.append_feedback(record)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to write to Google Sheets: %s", exc)
                await message.answer("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РІ Google Sheets. РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ СѓРІРµРґРѕРјР»РµРЅ.")
                await ctx.feedback_buffer.add(record)
        else:
            await ctx.feedback_buffer.add(record)
            logger.warning("Google Sheets client not configured. Feedback buffered locally.")
        await state.clear()
        await message.answer("РЎРїР°СЃРёР±Рѕ Р·Р° РѕР±СЂР°С‚РЅСѓСЋ СЃРІСЏР·СЊ! Р­С‚Рѕ РѕС‡РµРЅСЊ РІР°Р¶РЅРѕ РґР»СЏ РЅР°С€РµР№ РєРѕРјР°РЅРґС‹.")

    @router.callback_query(lambda c: c.data == "confirm_feedback_yes")
    async def confirm_feedback_yes(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await _finalize_feedback(callback.message, state)

    @router.callback_query(lambda c: c.data == "confirm_feedback_no")
    async def confirm_feedback_no(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await callback.message.answer("РћС‚Р·С‹РІ РѕС‚РјРµРЅРµРЅ.")

    @router.message(FeedbackStates.confirm)
    async def confirm(message: Message, state: FSMContext) -> None:
        decision = (message.text or "").strip().lower()
        if decision not in {"yes", "no", "РґР°", "РЅРµС‚"}:
            await message.answer("РћС‚РІРµС‚СЊС‚Рµ 'РґР°' С‡С‚РѕР±С‹ СЃРѕС…СЂР°РЅРёС‚СЊ РёР»Рё 'РЅРµС‚' С‡С‚РѕР±С‹ РѕС‚РјРµРЅРёС‚СЊ.")
            return
        if decision in {"no", "РЅРµС‚"}:
            await state.clear()
            await message.answer("РћС‚Р·С‹РІ РѕС‚РјРµРЅРµРЅ.")
            return

        await _finalize_feedback(message, state)






