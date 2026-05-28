from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import asyncio

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
    recommendations = State()
    overall_rating = State()
    recruiter = State()
    comms_rating = State()
    requirement_understanding = State()
    first_candidate_relevant = State()
    timeliness_rating = State()
    relevance_rating = State()
    process_clarity_rating = State()
    improvement_priority = State()
    recommend_recruiter = State()
    confirm = State()


def feedback_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оставить отзыв сейчас", callback_data=f"start_feedback:{vacancy_id}")],
            [InlineKeyboardButton(text="Напомнить позже", callback_data=f"remind_feedback:{vacancy_id}")],
        ]
    )


def recruiter_choice_keyboard(recruiter_name: str) -> InlineKeyboardMarkup:
    label = recruiter_name.strip() if recruiter_name.strip() else "Не знаю"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Использовать: {label}", callback_data="recruiter_use_default")],
            [InlineKeyboardButton(text="Ввести другое имя", callback_data="recruiter_other")],
        ]
    )


def confirm_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, сохранить", callback_data="confirm_feedback_yes")],
            [InlineKeyboardButton(text="Нет, отменить", callback_data="confirm_feedback_no")],
        ]
    )



def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data=f"{prefix}:yes")],
            [InlineKeyboardButton(text="Нет", callback_data=f"{prefix}:no")],
        ]
    )


def requirement_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="requirement:yes")],
            [InlineKeyboardButton(text="Частично", callback_data="requirement:partly")],
            [InlineKeyboardButton(text="Нет", callback_data="requirement:no")],
        ]
    )


def rating_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=str(value), callback_data=f"rating:{value}") for value in range(1, 6)]
        ]
    )


def next_daily_moscow(hour: int = 10) -> datetime:
    tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    target = datetime.combine(now.date(), time(hour=hour, minute=0, tzinfo=tz))
    if now >= target:
        target = target + timedelta(days=1)
    return target


def build_friendwork_job_url(vacancy_id: str | None) -> str | None:
    if not vacancy_id:
        return None
    return f"https://app.friend.work/Job/Edit/{vacancy_id}"


def ensure_vacancy_feedback_metadata(
    vacancy: VacancyAssignment | None, fallback_vacancy_id: str | None = None
) -> VacancyAssignment | None:
    if vacancy is None:
        return None
    vacancy_id = vacancy.vacancy_id or fallback_vacancy_id or ""
    if vacancy_id and vacancy_id != "manual":
        if not vacancy.job_url:
            vacancy.job_url = build_friendwork_job_url(vacancy_id)
        if not vacancy.closed_date:
            vacancy.closed_date = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%y")
    return vacancy


async def send_feedback_request(bot: Bot, ctx: AppContext, vacancy: VacancyAssignment) -> None:
    for manager_id in vacancy.hiring_manager_ids:
        user = await ctx.user_store.get(manager_id)
        if user and user.status != "active":
            continue
        existing = await ctx.reminders.get(manager_id, vacancy.vacancy_id)
        if existing:
            logger.info(
                "Skipping duplicate feedback request for vacancy %s and manager %s; reminder already exists.",
                vacancy.vacancy_id,
                manager_id,
            )
            continue
        text = (
            f"Вакансия закрыта: {vacancy.vacancy_title}\n"
            f"Рекрутер: {vacancy.recruiter_name}\n"
            "Можете оставить отзыв сейчас?"
        )
        sent = False
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                await bot.send_message(manager_id, text, reply_markup=feedback_keyboard(vacancy.vacancy_id))
                sent = True
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "Failed to send feedback request to %s on attempt %s/3: %s",
                    manager_id,
                    attempt,
                    exc,
                )
                if attempt < 3:
                    await asyncio.sleep(2)
        if not sent:
            logger.warning("Giving up sending feedback request to %s after 3 attempts: %s", manager_id, last_exc)
            continue
        now = datetime.now(ZoneInfo("Europe/Moscow"))
        next_at = next_daily_moscow()
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
        f"Вакансия закрыта: {vacancy.vacancy_title}\n"
        f"Рекрутер: {vacancy.recruiter_name}\n"
        "Можете оставить отзыв сейчас?"
    )
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            await bot.send_message(manager_id, text, reply_markup=feedback_keyboard(vacancy.vacancy_id))
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "Failed to send feedback request to %s on attempt %s/3: %s",
                manager_id,
                attempt,
                exc,
            )
            if attempt < 3:
                await asyncio.sleep(2)
    logger.warning("Giving up sending feedback request to %s after 3 attempts: %s", manager_id, last_exc)


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
        await message.answer(
            "Здравствуйте! Используйте /register, чтобы подтвердить доступ, или ждите запрос на отзыв."
        )

    @router.message(Command("register"))
    async def register(message: Message, state: FSMContext) -> None:
        existing = await get_registered_user(message.from_user.id, message.from_user.username)
        if existing:
            await message.answer("Вы уже зарегистрированы. Спасибо!")
            return
        await state.set_state(RegistrationStates.waiting_full_name)
        await message.answer("Укажите ваше ФИО для завершения регистрации.")

    @router.message(RegistrationStates.waiting_full_name)
    async def save_full_name(message: Message, state: FSMContext) -> None:
        await state.update_data(full_name=message.text.strip())
        await state.set_state(RegistrationStates.waiting_title)
        await message.answer("Ваша должность?")

    @router.message(RegistrationStates.waiting_title)
    async def save_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text.strip())
        await state.set_state(RegistrationStates.waiting_contact)
        await message.answer("Контакт (email/телефон)?")

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
        await message.answer("Вы успешно зарегистрированы. Спасибо!")

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
            await callback.message.answer("Не могу найти вакансию. Напишите администратору.")
            return
        vacancy = ensure_vacancy_feedback_metadata(vacancy, vacancy_id)
        await ctx.vacancy_store.upsert(vacancy)
        user = await get_registered_user(callback.from_user.id, callback.from_user.username)
        if not user:
            await callback.message.answer(
                f"Вы не зарегистрированы. Напишите администратору ({ctx.settings.admin_contact})."
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
        await state.set_state(FeedbackStates.recommendations)
        await callback.message.answer("Какие рекомендации по улучшению работы рекрутера? Отправьте текст или голос.")

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
            f"Хорошо, напомню {remind_at.strftime('%d.%m.%Y в %H:%M')} МСК."
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
                f"Вы не зарегистрированы. Напишите администратору ({ctx.settings.admin_contact})."
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
        await state.set_state(FeedbackStates.recommendations)
        await message.answer("Запускаю ручной опрос. Какие рекомендации по улучшению работы рекрутера? Отправьте текст или голос.")

    async def _validate_rating(message: Message, min_value: int = 1, max_value: int = 5) -> int | None:
        try:
            value = int(message.text.strip())
            if min_value <= value <= max_value:
                return value
        except Exception:
            pass
        await message.answer(f"Введите число от {min_value} до {max_value}.")
        return None

    async def _read_text_or_voice(message: Message, bot: Bot, empty_prompt: str) -> str | None:
        if message.voice:
            if ctx.speech is None:
                await message.answer("Распознавание речи не настроено. Отправьте текстом, пожалуйста.")
                return None
            await message.answer("Преобразуем голосовое в текст...")
            file = await bot.get_file(message.voice.file_id)
            buffer = await bot.download_file(file.file_path)
            transcription = await ctx.speech.transcribe_bytes(buffer.read())
            if not transcription:
                await message.answer("Не удалось распознать голос. Отправьте текстом, пожалуйста.")
                return None
            await message.answer(f"Транскрипция:\n{transcription}")
            return transcription
        if message.text:
            return message.text.strip()
        await message.answer(empty_prompt)
        return None

    @router.message(FeedbackStates.overall_rating)
    async def receive_overall(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(overall_rating=rating)
        await state.set_state(FeedbackStates.comms_rating)
        await message.answer("Как оцениваете коммуникацию с рекрутером? (1-5)", reply_markup=rating_keyboard())

    @router.callback_query(lambda c: c.data and c.data.startswith("rating:"))
    async def receive_rating_callback(callback: CallbackQuery, state: FSMContext) -> None:
        current_state = await state.get_state()
        if current_state not in {
            FeedbackStates.overall_rating.state,
            FeedbackStates.comms_rating.state,
            FeedbackStates.relevance_rating.state,
            FeedbackStates.process_clarity_rating.state,
        }:
            await callback.answer()
            return

        try:
            rating = int(callback.data.split(":", maxsplit=1)[1])
        except (TypeError, ValueError):
            await callback.answer("Некорректная оценка", show_alert=True)
            return

        if rating < 1 or rating > 5:
            await callback.answer("Оценка должна быть от 1 до 5", show_alert=True)
            return

        await callback.answer()

        if current_state == FeedbackStates.overall_rating.state:
            await state.update_data(overall_rating=rating)
            await state.set_state(FeedbackStates.comms_rating)
            await callback.message.answer("Как оцениваете коммуникацию с рекрутером? (1-5)", reply_markup=rating_keyboard())
            return

        if current_state == FeedbackStates.comms_rating.state:
            await state.update_data(comms_rating=rating)
            await state.set_state(FeedbackStates.requirement_understanding)
            await callback.message.answer(
                "Понял ли рекрутер требования? Да/Частично/Нет",
                reply_markup=requirement_keyboard(),
            )
            return

        if current_state == FeedbackStates.relevance_rating.state:
            await state.update_data(relevance_rating=rating)
            await state.set_state(FeedbackStates.process_clarity_rating)
            await callback.message.answer(
                "Насколько понятен был процесс? (1-5)",
                reply_markup=rating_keyboard(),
            )
            return

        await state.update_data(process_clarity_rating=rating)
        await state.set_state(FeedbackStates.timeliness_rating)
        await callback.message.answer(
            "Вакансия закрыта в комфортные сроки? Да/Нет",
            reply_markup=yes_no_keyboard("timeliness"),
        )

    @router.callback_query(lambda c: c.data == "recruiter_use_default")
    async def recruiter_use_default(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        data = await state.get_data()
        recruiter_name = data.get("recruiter_name", "Неизвестен")
        await state.update_data(recruiter_name=recruiter_name)
        await state.set_state(FeedbackStates.overall_rating)
        await callback.message.answer("Общая оценка работы рекрутера (1-5)?", reply_markup=rating_keyboard())

    @router.callback_query(lambda c: c.data == "recruiter_other")
    async def recruiter_other(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.set_state(FeedbackStates.recruiter)
        await callback.message.answer("Введите имя рекрутера.")

    @router.message(FeedbackStates.recruiter)
    async def receive_recruiter(message: Message, state: FSMContext) -> None:
        recruiter_name = message.text.strip()
        await state.update_data(recruiter_name=recruiter_name)
        await state.set_state(FeedbackStates.overall_rating)
        await message.answer("Общая оценка работы рекрутера (1-5)?", reply_markup=rating_keyboard())

    @router.message(FeedbackStates.comms_rating)
    async def receive_comms(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(comms_rating=rating)
        await state.set_state(FeedbackStates.requirement_understanding)
        await message.answer("Понял ли рекрутер требования? Да/Частично/Нет", reply_markup=requirement_keyboard())

    @router.callback_query(lambda c: c.data and c.data.startswith("requirement:"))
    async def receive_requirement_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        value = callback.data.split(":", maxsplit=1)[1]
        labels = {"yes": "да", "partly": "частично", "no": "нет"}
        if value not in labels:
            await callback.answer("Некорректный ответ", show_alert=True)
            return
        await state.update_data(requirement_understanding=labels[value])
        await state.set_state(FeedbackStates.first_candidate_relevant)
        await callback.message.answer(
            "Первый кандидат был релевантен? Да/Нет",
            reply_markup=yes_no_keyboard("first_candidate"),
        )

    @router.message(FeedbackStates.requirement_understanding)
    async def receive_requirement_text(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip().lower()
        labels = {"да": "да", "yes": "да", "частично": "частично", "нет": "нет", "no": "нет"}
        if value not in labels:
            await message.answer("Ответьте 'Да', 'Частично' или 'Нет'.")
            return
        await state.update_data(requirement_understanding=labels[value])
        await state.set_state(FeedbackStates.first_candidate_relevant)
        await message.answer("Первый кандидат был релевантен? Да/Нет", reply_markup=yes_no_keyboard("first_candidate"))

    @router.callback_query(lambda c: c.data and c.data.startswith("first_candidate:"))
    async def receive_first_candidate_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        value = callback.data.split(":", maxsplit=1)[1]
        if value not in {"yes", "no"}:
            await callback.answer("Некорректный ответ", show_alert=True)
            return
        await state.update_data(first_candidate_relevant="да" if value == "yes" else "нет")
        await state.set_state(FeedbackStates.relevance_rating)
        await callback.message.answer("Насколько релевантны кандидаты? (1-5)", reply_markup=rating_keyboard())

    @router.message(FeedbackStates.first_candidate_relevant)
    async def receive_first_candidate_text(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip().lower()
        if value not in {"да", "нет", "yes", "no"}:
            await message.answer("Ответьте 'Да' или 'Нет'.")
            return
        await state.update_data(first_candidate_relevant="да" if value in {"да", "yes"} else "нет")
        await state.set_state(FeedbackStates.relevance_rating)
        await message.answer("Насколько релевантны кандидаты? (1-5)", reply_markup=rating_keyboard())

    @router.callback_query(lambda c: c.data and c.data.startswith("timeliness:"))
    async def receive_time_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        value = callback.data.split(":", maxsplit=1)[1]
        if value not in {"yes", "no"}:
            await callback.answer("Некорректный ответ", show_alert=True)
            return
        value = "да" if value == "yes" else "нет"
        await state.update_data(timeliness_rating=value)
        await state.set_state(FeedbackStates.improvement_priority)
        await callback.message.answer("Что улучшить в первую очередь? Отправьте текст или голос.")

    @router.message(FeedbackStates.timeliness_rating)
    async def receive_time_text(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip().lower()
        if value not in {"да", "нет", "yes", "no"}:
            await message.answer("Ответьте 'Да' или 'Нет'.")
            return
        normalized = "да" if value in {"да", "yes"} else "нет"
        await state.update_data(timeliness_rating=normalized)
        await state.set_state(FeedbackStates.improvement_priority)
        await message.answer("Что улучшить в первую очередь? Отправьте текст или голос.")
    @router.message(FeedbackStates.relevance_rating)
    async def receive_relevance(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(relevance_rating=rating)
        await state.set_state(FeedbackStates.process_clarity_rating)
        await message.answer(
            "Насколько понятен был процесс? (1-5)",
            reply_markup=rating_keyboard(),
        )

    @router.message(FeedbackStates.process_clarity_rating)
    async def receive_process_clarity(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(process_clarity_rating=rating)
        await state.set_state(FeedbackStates.timeliness_rating)
        await message.answer("Вакансия закрыта в комфортные сроки? Да/Нет", reply_markup=yes_no_keyboard("timeliness"))

    @router.message(FeedbackStates.recommendations)
    async def receive_recommendations(message: Message, state: FSMContext, bot: Bot) -> None:
        text = await _read_text_or_voice(message, bot, "Отправьте текст или голосовое с рекомендациями.")
        if not text:
            return

        await state.update_data(recommendations=text, feedback_comment=text)
        data = await state.get_data()
        await state.set_state(FeedbackStates.recruiter)
        await message.answer(
            "С каким рекрутером вы работали?",
            reply_markup=recruiter_choice_keyboard(data.get("recruiter_name", "")),
        )

    @router.message(FeedbackStates.improvement_priority)
    async def receive_improvement_priority(message: Message, state: FSMContext, bot: Bot) -> None:
        text = await _read_text_or_voice(message, bot, "Отправьте текст или голосовое с тем, что нужно улучшить.")
        if not text:
            return
        await state.update_data(improvement_priority=text)
        await state.set_state(FeedbackStates.recommend_recruiter)
        await message.answer("Рекомендовали бы рекрутера? Да/Нет", reply_markup=yes_no_keyboard("recommend_recruiter"))

    @router.callback_query(lambda c: c.data and c.data.startswith("recommend_recruiter:"))
    async def receive_recommend_recruiter_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        value = callback.data.split(":", maxsplit=1)[1]
        if value not in {"yes", "no"}:
            await callback.answer("Некорректный ответ", show_alert=True)
            return
        await state.update_data(recommend_recruiter="да" if value == "yes" else "нет")
        await _show_feedback_summary(callback.message, state)

    @router.message(FeedbackStates.recommend_recruiter)
    async def receive_recommend_recruiter_text(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip().lower()
        if value not in {"да", "нет", "yes", "no"}:
            await message.answer("Ответьте 'Да' или 'Нет'.")
            return
        await state.update_data(recommend_recruiter="да" if value in {"да", "yes"} else "нет")
        await _show_feedback_summary(message, state)

    async def _show_feedback_summary(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.set_state(FeedbackStates.confirm)
        summary = (
            f"Вакансия: {data.get('vacancy_title')}\n"
            f"Рекрутер: {data.get('recruiter_name')}\n"
            f"Рекомендации: {data.get('recommendations')}\n"
            f"Общая оценка: {data.get('overall_rating')}\n"
            f"Коммуникация: {data.get('comms_rating')}\n"
            f"Понял требования: {data.get('requirement_understanding')}\n"
            f"Первый кандидат релевантен: {data.get('first_candidate_relevant')}\n"
            f"Релевантность кандидатов: {data.get('relevance_rating')}\n"
            f"Понятность процесса: {data.get('process_clarity_rating')}\n"
            f"Сроки закрытия: {data.get('timeliness_rating')}\n"
            f"Что улучшить: {data.get('improvement_priority')}\n"
            f"Рекомендовали бы рекрутера: {data.get('recommend_recruiter')}\n\n"
            "Сохранить отзыв? Ответьте 'да' для сохранения или 'нет' для отмены."
        )
        await message.answer(summary, reply_markup=confirm_feedback_keyboard())

    async def _finalize_feedback(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        vacancy_id = data.get("vacancy_id", "")
        closed_date = data.get("closed_date", "")
        job_url = data.get("job_url", "")
        if vacancy_id and vacancy_id != "manual":
            if not closed_date:
                closed_date = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%y")
            if not job_url:
                job_url = build_friendwork_job_url(vacancy_id) or ""
        await ctx.reminders.remove(message.from_user.id, vacancy_id)
        record = FeedbackRecord(
            vacancy_id=data.get("vacancy_id", ""),
            vacancy_title=data.get("vacancy_title", ""),
            recruiter_name=data.get("recruiter_name", ""),
            hiring_manager_full_name=data.get("hiring_manager_full_name", message.from_user.full_name or ""),
            closed_date=closed_date,
            job_url=job_url,
            candidate_count=int(data.get("candidate_count") or 0),
            tech_interview_count=int(data.get("tech_interview_count") or 0),
            telegram_user_id=message.from_user.id,
            feedback_comment=data.get("feedback_comment", ""),
            overall_rating=data.get("overall_rating", 0),
            comms_rating=data.get("comms_rating", 0),
            requirement_understanding=str(data.get("requirement_understanding") or ""),
            first_candidate_relevant=str(data.get("first_candidate_relevant") or ""),
            timeliness_rating=str(data.get("timeliness_rating") or ""),
            relevance_rating=data.get("relevance_rating", 0),
            process_quality_rating=data.get("process_quality_rating", 0),
            process_clarity_rating=data.get("process_clarity_rating", 0),
            recommendations=data.get("recommendations", ""),
            improvement_priority=data.get("improvement_priority", ""),
            recommend_recruiter=str(data.get("recommend_recruiter") or ""),
            submitted_at=datetime.utcnow(),
        )
        if ctx.sheets:
            try:
                logger.info(
                    "Feedback payload preview: vacancy_id=%s closed_date=%s job_url=%s",
                    record.vacancy_id,
                    record.closed_date,
                    record.job_url,
                )
                ctx.sheets.append_feedback(record)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to write to Google Sheets: %s", exc)
                await message.answer("Не удалось сохранить в Google Sheets. Администратор уведомлен.")
                await ctx.feedback_buffer.add(record)
        else:
            await ctx.feedback_buffer.add(record)
            logger.warning("Google Sheets client not configured. Feedback buffered locally.")
        await state.clear()
        await message.answer("Спасибо за обратную связь! Это очень важно для нашей команды.")

    @router.callback_query(lambda c: c.data == "confirm_feedback_yes")
    async def confirm_feedback_yes(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await _finalize_feedback(callback.message, state)

    @router.callback_query(lambda c: c.data == "confirm_feedback_no")
    async def confirm_feedback_no(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await callback.message.answer("Отзыв отменен.")

    @router.message(FeedbackStates.confirm)
    async def confirm(message: Message, state: FSMContext) -> None:
        decision = (message.text or "").strip().lower()
        if decision not in {"yes", "no", "да", "нет"}:
            await message.answer("Ответьте 'да' чтобы сохранить или 'нет' чтобы отменить.")
            return
        if decision in {"no", "нет"}:
            await state.clear()
            await message.answer("Отзыв отменен.")
            return

        await _finalize_feedback(message, state)






