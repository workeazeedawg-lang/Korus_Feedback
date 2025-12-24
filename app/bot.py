from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

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
from .storage import FeedbackBuffer, UserStore, VacancyStore

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    user_store: UserStore
    vacancy_store: VacancyStore
    feedback_buffer: FeedbackBuffer
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
            [InlineKeyboardButton(text="Оставить отзыв сейчас", callback_data=f"start_feedback:{vacancy_id}")],
            [InlineKeyboardButton(text="Напомнить позже", callback_data=f"remind_feedback:{vacancy_id}")],
        ]
    )


async def send_feedback_request(bot: Bot, ctx: AppContext, vacancy: VacancyAssignment) -> None:
    for manager_id in vacancy.hiring_manager_ids:
        user = await ctx.user_store.get(manager_id)
        if user and user.status != "active":
            continue
        text = (
            f"Вакансия закрыта: {vacancy.vacancy_title}\n"
            f"Рекрутер: {vacancy.recruiter_name}\n"
            "Можете оставить отзыв сейчас?"
        )
        try:
            await bot.send_message(manager_id, text, reply_markup=feedback_keyboard(vacancy.vacancy_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send feedback request to %s: %s", manager_id, exc)


def register_handlers(router: Router, ctx: AppContext) -> None:
    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer("Здравствуйте! Используйте /register, чтобы подтвердить доступ, или ждите запрос на отзыв.")

    @router.message(Command("register"))
    async def register(message: Message, state: FSMContext) -> None:
        existing = await ctx.user_store.get(message.from_user.id)
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
            title=data.get("title"),
            contact=message.text.strip(),
            permission_level="hiring_manager",
        )
        await ctx.user_store.upsert(user)
        await state.clear()
        await message.answer("Вы успешно зарегистрированы. Спасибо!")

    @router.callback_query(lambda c: c.data and c.data.startswith("start_feedback:"))
    async def handle_start_feedback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        vacancy_id = callback.data.split(":", maxsplit=1)[1]
        vacancy = await ctx.vacancy_store.get(vacancy_id)
        if not vacancy:
            await callback.message.answer("Не могу найти вакансию. Напишите администратору.")
            return
        user = await ctx.user_store.get(callback.from_user.id)
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
        )
        await state.set_state(FeedbackStates.overall_rating)
        await callback.message.answer("Общая оценка работы рекрутера (1-5)?")

    @router.callback_query(lambda c: c.data and c.data.startswith("remind_feedback:"))
    async def handle_remind_feedback(callback: CallbackQuery) -> None:
        await callback.answer("Хорошо, напомню позже.")
        # Hook for reminder scheduling can be added here.

    @router.message(Command("feedback"))
    async def manual_feedback(message: Message, state: FSMContext) -> None:
        vacancy = VacancyAssignment(
            vacancy_id="manual",
            vacancy_title="Manual trigger",
            recruiter_name="Unknown",
            hiring_manager_ids=[message.from_user.id],
        )
        user = await ctx.user_store.get(message.from_user.id)
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
        )
        await state.set_state(FeedbackStates.overall_rating)
        await message.answer("Запускаю ручной опрос. Общая оценка работы рекрутера (1-5)?")

    async def _validate_rating(message: Message, min_value: int = 1, max_value: int = 5) -> int | None:
        try:
            value = int(message.text.strip())
            if min_value <= value <= max_value:
                return value
        except Exception:
            pass
        await message.answer(f"Введите число от {min_value} до {max_value}.")
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
            f"С каким рекрутером вы работали? (по умолчанию: {data.get('recruiter_name')})\n"
            "Введите имя или напишите 'по умолчанию'."
        )

    @router.message(FeedbackStates.recruiter)
    async def receive_recruiter(message: Message, state: FSMContext) -> None:
        recruiter_name = message.text.strip()
        data = await state.get_data()
        if recruiter_name.lower() == "default":
            recruiter_name = data.get("recruiter_name", "Unknown")
        await state.update_data(recruiter_name=recruiter_name)
        await state.set_state(FeedbackStates.comms_rating)
        await message.answer("Как оцениваете коммуникацию с рекрутером? (1-5)")

    @router.message(FeedbackStates.comms_rating)
    async def receive_comms(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(comms_rating=rating)
        await state.set_state(FeedbackStates.timeliness_rating)
        await message.answer("Вакансия закрыта в комфортные сроки? (1-5)")

    @router.message(FeedbackStates.timeliness_rating)
    async def receive_time(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(timeliness_rating=rating)
        await state.set_state(FeedbackStates.relevance_rating)
        await message.answer("Насколько релевантны кандидаты? (1-5)")

    @router.message(FeedbackStates.relevance_rating)
    async def receive_relevance(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(relevance_rating=rating)
        await state.set_state(FeedbackStates.process_quality_rating)
        await message.answer("Как оцениваете качество процесса (ошибки, фидбек, поддержка, HR-интервью)? (1-5)")

    @router.message(FeedbackStates.process_quality_rating)
    async def receive_process_quality(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(process_quality_rating=rating)
        await state.set_state(FeedbackStates.recommendations)
        await message.answer("Какие рекомендации по улучшению работы рекрутера? Отправьте текст или голос.")

    @router.message(FeedbackStates.recommendations)
    async def receive_recommendations(message: Message, state: FSMContext, bot: Bot) -> None:
        text: str | None = None
        if message.voice:
            if ctx.speech is None:
                await message.answer("Распознавание речи не настроено. Отправьте текстом, пожалуйста.")
                return
            await message.answer("Преобразуем голосовое в текст...")
            file = await bot.get_file(message.voice.file_id)
            buffer = await bot.download_file(file.file_path)
            transcription = await ctx.speech.transcribe_bytes(buffer.read())
            if not transcription:
                await message.answer("Не удалось распознать голос. Отправьте текстом, пожалуйста.")
                return
            text = transcription
            await message.answer(f"Транскрипция:\n{text}")
        elif message.text:
            text = message.text.strip()

        if not text:
            await message.answer("Отправьте текст или голосовое с рекомендациями.")
            return

        await state.update_data(recommendations=text, feedback_comment=text)
        data = await state.get_data()
        await state.set_state(FeedbackStates.confirm)
        summary = (
            f"Вакансия: {data.get('vacancy_title')}\n"
            f"Рекрутер: {data.get('recruiter_name')}\n"
            f"Общая оценка: {data.get('overall_rating')}\n"
            f"Коммуникация: {data.get('comms_rating')}\n"
            f"Сроки закрытия: {data.get('timeliness_rating')}\n"
            f"Релевантность кандидатов: {data.get('relevance_rating')}\n"
            f"Качество процесса: {data.get('process_quality_rating')}\n"
            f"Рекомендации: {text}\n\n"
            "Сохранить отзыв? Ответьте 'да' для сохранения или 'нет' для отмены."
        )
        await message.answer(summary)

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

        data = await state.get_data()
        record = FeedbackRecord(
            vacancy_id=data.get("vacancy_id", ""),
            vacancy_title=data.get("vacancy_title", ""),
            recruiter_name=data.get("recruiter_name", ""),
            hiring_manager_full_name=data.get("hiring_manager_full_name", message.from_user.full_name or ""),
            telegram_user_id=message.from_user.id,
            feedback_comment=data.get("feedback_comment", ""),
            overall_rating=data.get("overall_rating", 0),
            comms_rating=data.get("comms_rating", 0),
            timeliness_rating=data.get("timeliness_rating", 0),
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
                await message.answer("Не удалось сохранить в Google Sheets. Администратор уведомлен.")
                await ctx.feedback_buffer.add(record)
        else:
            await ctx.feedback_buffer.add(record)
            logger.warning("Google Sheets client not configured. Feedback buffered locally.")
        await state.clear()
        await message.answer("Спасибо за обратную связь! Это очень важно для нашей команды.")

