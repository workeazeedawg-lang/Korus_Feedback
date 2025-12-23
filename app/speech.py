import asyncio
import io
import logging
from typing import Optional

from google.cloud import speech

logger = logging.getLogger(__name__)


class SpeechToText:
    def __init__(self, language_code: str = "en-US") -> None:
        self.language_code = language_code
        self._client = speech.SpeechClient()

    async def transcribe_bytes(self, audio_bytes: bytes) -> Optional[str]:
        def _recognize() -> Optional[str]:
            audio = speech.RecognitionAudio(content=audio_bytes)
            config = speech.RecognitionConfig(language_code=self.language_code, enable_automatic_punctuation=True)
            response = self._client.recognize(config=config, audio=audio)
            transcripts = [result.alternatives[0].transcript for result in response.results if result.alternatives]
            return " ".join(transcripts).strip() if transcripts else None

        return await asyncio.to_thread(_recognize)
