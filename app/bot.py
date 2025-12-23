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
            [InlineKeyboardButton(text="Provide feedback now", callback_data=f"start_feedback:{vacancy_id}")],
            [InlineKeyboardButton(text="Remind me later", callback_data=f"remind_feedback:{vacancy_id}")],
        ]
    )


async def send_feedback_request(bot: Bot, ctx: AppContext, vacancy: VacancyAssignment) -> None:
    for manager_id in vacancy.hiring_manager_ids:
        user = await ctx.user_store.get(manager_id)
        if user and user.status != "active":
            continue
        text = (
            f"Vacancy closed: {vacancy.vacancy_title}\n"
            f"Recruiter: {vacancy.recruiter_name}\n"
            "Can you leave feedback now?"
        )
        try:
            await bot.send_message(manager_id, text, reply_markup=feedback_keyboard(vacancy.vacancy_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send feedback request to %s: %s", manager_id, exc)


def register_handlers(router: Router, ctx: AppContext) -> None:
    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer("Hello! Use /register to confirm access or wait for a feedback request.")

    @router.message(Command("register"))
    async def register(message: Message, state: FSMContext) -> None:
        existing = await ctx.user_store.get(message.from_user.id)
        if existing:
            await message.answer("Welcome back! You are already registered.")
            return
        await state.set_state(RegistrationStates.waiting_full_name)
        await message.answer("Please share your full name to finish registration.")

    @router.message(RegistrationStates.waiting_full_name)
    async def save_full_name(message: Message, state: FSMContext) -> None:
        await state.update_data(full_name=message.text.strip())
        await state.set_state(RegistrationStates.waiting_title)
        await message.answer("Your job title?")

    @router.message(RegistrationStates.waiting_title)
    async def save_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text.strip())
        await state.set_state(RegistrationStates.waiting_contact)
        await message.answer("Contact details (email/phone)?")

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
        await message.answer("Welcome! You have been successfully registered.")

    @router.callback_query(lambda c: c.data and c.data.startswith("start_feedback:"))
    async def handle_start_feedback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        vacancy_id = callback.data.split(":", maxsplit=1)[1]
        vacancy = await ctx.vacancy_store.get(vacancy_id)
        if not vacancy:
            await callback.message.answer("Sorry, I cannot find this vacancy. Please contact the administrator.")
            return
        user = await ctx.user_store.get(callback.from_user.id)
        if not user:
            await callback.message.answer(
                f"You are not registered. Please contact the administrator ({ctx.settings.admin_contact})."
            )
            return
        await state.update_data(
            vacancy_id=vacancy.vacancy_id,
            vacancy_title=vacancy.vacancy_title,
            recruiter_name=vacancy.recruiter_name,
            hiring_manager_full_name=user.full_name,
        )
        await state.set_state(FeedbackStates.overall_rating)
        await callback.message.answer("Overall performance rating (1-5)?")

    @router.callback_query(lambda c: c.data and c.data.startswith("remind_feedback:"))
    async def handle_remind_feedback(callback: CallbackQuery) -> None:
        await callback.answer("Okay, I'll remind you later.")
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
                f"You are not registered. Please contact the administrator ({ctx.settings.admin_contact})."
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
        await message.answer("Starting manual feedback. Overall performance rating (1-5)?")

    async def _validate_rating(message: Message, min_value: int = 1, max_value: int = 5) -> int | None:
        try:
            value = int(message.text.strip())
            if min_value <= value <= max_value:
                return value
        except Exception:
            pass
        await message.answer(f"Please provide a number between {min_value} and {max_value}.")
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
            f"Which recruiter did you work with? (default: {data.get('recruiter_name')}) "
            "Reply with the name or type 'default'."
        )

    @router.message(FeedbackStates.recruiter)
    async def receive_recruiter(message: Message, state: FSMContext) -> None:
        recruiter_name = message.text.strip()
        data = await state.get_data()
        if recruiter_name.lower() == "default":
            recruiter_name = data.get("recruiter_name", "Unknown")
        await state.update_data(recruiter_name=recruiter_name)
        await state.set_state(FeedbackStates.comms_rating)
        await message.answer("How would you rate communication with the recruiter? (1-5)")

    @router.message(FeedbackStates.comms_rating)
    async def receive_comms(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(comms_rating=rating)
        await state.set_state(FeedbackStates.timeliness_rating)
        await message.answer("Was the vacancy closed within a comfortable timeframe? (1-5)")

    @router.message(FeedbackStates.timeliness_rating)
    async def receive_time(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(timeliness_rating=rating)
        await state.set_state(FeedbackStates.relevance_rating)
        await message.answer("How relevant were the candidates provided? (1-5)")

    @router.message(FeedbackStates.relevance_rating)
    async def receive_relevance(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(relevance_rating=rating)
        await state.set_state(FeedbackStates.process_quality_rating)
        await message.answer("Rate the quality of the recruitment process (1-5)")

    @router.message(FeedbackStates.process_quality_rating)
    async def receive_process_quality(message: Message, state: FSMContext) -> None:
        rating = await _validate_rating(message)
        if rating is None:
            return
        await state.update_data(process_quality_rating=rating)
        await state.set_state(FeedbackStates.recommendations)
        await message.answer("What recommendations do you have to improve the recruiter’s work? Send text or voice.")

    @router.message(FeedbackStates.recommendations)
    async def receive_recommendations(message: Message, state: FSMContext, bot: Bot) -> None:
        text: str | None = None
        if message.voice:
            if ctx.speech is None:
                await message.answer("Speech-to-text is not configured. Please send text feedback.")
                return
            await message.answer("Transcribing your voice message...")
            file = await bot.get_file(message.voice.file_id)
            buffer = await bot.download_file(file.file_path)
            transcription = await ctx.speech.transcribe_bytes(buffer.read())
            if not transcription:
                await message.answer("Sorry, I couldn't transcribe that. Please send text feedback.")
                return
            text = transcription
            await message.answer(f"Transcript:\n{text}")
        elif message.text:
            text = message.text.strip()

        if not text:
            await message.answer("Please send text or a voice message with your recommendations.")
            return

        await state.update_data(recommendations=text, feedback_comment=text)
        data = await state.get_data()
        await state.set_state(FeedbackStates.confirm)
        summary = (
            f"Vacancy: {data.get('vacancy_title')}\n"
            f"Recruiter: {data.get('recruiter_name')}\n"
            f"Overall: {data.get('overall_rating')}\n"
            f"Communication: {data.get('comms_rating')}\n"
            f"Timeliness: {data.get('timeliness_rating')}\n"
            f"Relevance: {data.get('relevance_rating')}\n"
            f"Process quality: {data.get('process_quality_rating')}\n"
            f"Recommendations: {text}\n\n"
            "Save this feedback? Reply 'yes' to confirm or 'no' to cancel."
        )
        await message.answer(summary)

    @router.message(FeedbackStates.confirm)
    async def confirm(message: Message, state: FSMContext) -> None:
        decision = (message.text or "").strip().lower()
        if decision not in {"yes", "no"}:
            await message.answer("Please reply 'yes' to save or 'no' to cancel.")
            return
        if decision == "no":
            await state.clear()
            await message.answer("Feedback canceled.")
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
                await message.answer("Could not save to Google Sheets. The admin has been notified.")
                await ctx.feedback_buffer.add(record)
        else:
            await ctx.feedback_buffer.add(record)
            logger.warning("Google Sheets client not configured. Feedback buffered locally.")
        await state.clear()
        await message.answer("Thank you for your feedback! It is very important to our team.")

