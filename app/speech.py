import asyncio
import io
import logging
import os
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


class SpeechToText:
    def __init__(self, language_code: str = "en-US") -> None:
        self.language_code = language_code
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        self._client = OpenAI(api_key=api_key)
        self._model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")

    async def transcribe_bytes(self, audio_bytes: bytes) -> Optional[str]:
        def _recognize() -> Optional[str]:
            audio = io.BytesIO(audio_bytes)
            audio.name = "audio.ogg"
            language = "ru" if self.language_code.lower().startswith("ru") else None
            response = self._client.audio.transcriptions.create(
                model=self._model,
                file=audio,
                language=language,
            )
            text = (response.text or "").strip()
            return text or None

        return await asyncio.to_thread(_recognize)
