import logging
from dataclasses import asdict
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import FeedbackRecord

logger = logging.getLogger(__name__)


class SheetWebhookClient:
    def __init__(self, webhook_url: str, webhook_key: str) -> None:
        self.webhook_url = webhook_url
        self.webhook_key = webhook_key

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
    def append_feedback(self, record: FeedbackRecord) -> None:
        params = {"key": self.webhook_key or ""}

        payload = {
            "vacancy": record.vacancy_title or record.vacancy_id,
            "vacancy_id": record.vacancy_id,
            "hiring_manager": record.hiring_manager_full_name,
            "comment": record.feedback_comment,
            "recruiter": record.recruiter_name,
            "overall": record.overall_rating,
            "overall_rating": record.overall_rating,
            "comms_rating": record.comms_rating,
            "timeliness_rating": record.timeliness_rating,
            "relevance_rating": record.relevance_rating,
            "process_quality_rating": record.process_quality_rating,
            "recommendations": record.recommendations,
            "submitted_at": record.submitted_at.isoformat(),
            "telegram_user_id": record.telegram_user_id,
            "source": "telegram-bot",
            # Row mapped to the new sheet headers (A-I):
            "row": [
                record.vacancy_title or record.vacancy_id,
                record.hiring_manager_full_name,
                record.recommendations or record.feedback_comment,
                record.recruiter_name,
                record.overall_rating,
                record.comms_rating,
                record.timeliness_rating,
                record.relevance_rating,
                record.process_quality_rating,
            ],
        }

        resp = httpx.post(
            self.webhook_url,
            params=params,
            json=payload,
            timeout=15,
            headers={"Content-Type": "application/json"},
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            logger.error("Sheet webhook failed (%s): %s", resp.status_code, resp.text)
            resp.raise_for_status()
        logger.info("Sent feedback to sheet webhook for vacancy %s (status %s)", record.vacancy_id, resp.status_code)


class GoogleSheetClient(SheetWebhookClient):
    """
    Backward-compatible client name. Uses the Apps Script webhook under the hood.
    """
