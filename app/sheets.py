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
        params = {}
        if self.webhook_key:
            params["key"] = self.webhook_key

        payload = {
            "vacancy": record.vacancy_title or record.vacancy_id,
            "hiring_manager": record.hiring_manager_full_name,
            "comment": record.feedback_comment,
            "recruiter": record.recruiter_name,
        }

        resp = httpx.post(self.webhook_url, params=params, json=payload, timeout=10)
        if resp.status_code >= 300:
            logger.error("Sheet webhook failed (%s): %s", resp.status_code, resp.text)
            resp.raise_for_status()
        logger.info("Sent feedback to sheet webhook for vacancy %s", record.vacancy_id)


class GoogleSheetClient(SheetWebhookClient):
    """
    Backward-compatible client name. Uses the Apps Script webhook under the hood.
    """
