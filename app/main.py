import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from .bot import AppContext, register_handlers
from .config import load_settings
from .friendwork import create_friendwork_router
from .sheets import GoogleSheetClient
from .speech import SpeechToText
from .storage import EventStore, FeedbackBuffer, UserStore, VacancyStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = load_settings()
bot = Bot(token=settings.telegram_token)
dp = Dispatcher(storage=MemoryStorage())

sheets_client: Optional[GoogleSheetClient] = None
if settings.sheets_webhook_url:
    sheets_client = GoogleSheetClient(settings.sheets_webhook_url, settings.sheets_webhook_key)
else:
    logger.warning("SHEETS_WEBHOOK_URL not set. Feedback will be buffered locally.")

speech_client: Optional[SpeechToText] = None
try:
    speech_client = SpeechToText(language_code=settings.speech_language_code)
except Exception as exc:  # noqa: BLE001
    logger.warning("Speech-to-text not initialized: %s", exc)

user_store = UserStore()
vacancy_store = VacancyStore()
feedback_buffer = FeedbackBuffer()
event_store = EventStore()

ctx = AppContext(
    settings=settings,
    user_store=user_store,
    vacancy_store=vacancy_store,
    feedback_buffer=feedback_buffer,
    sheets=sheets_client,
    speech=speech_client,
)

router = create_friendwork_router(ctx, bot, dp, event_store)
register_handlers(dp, ctx)

app = FastAPI()
app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post(settings.telegram_webhook_path)
async def telegram_webhook(request: Request) -> dict:
    secret = request.headers.get("x-telegram-bot-api-secret-token")
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid Telegram secret token")
    update = Update.model_validate(await request.json())
    await dp.feed_update(bot, update)
    return {"status": "ok"}


# This allows running polling in development if you prefer.
async def run_polling() -> None:
    await dp.start_polling(bot)


__all__ = ["app", "bot", "dp", "run_polling"]
