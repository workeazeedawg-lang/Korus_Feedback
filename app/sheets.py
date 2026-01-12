import logging
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import FeedbackRecord, User, VacancyAssignment

logger = logging.getLogger(__name__)


class SheetWebhookClient:
    def __init__(self, webhook_url: str, webhook_key: str) -> None:
        self.webhook_url = webhook_url
        self.webhook_key = webhook_key

    def _post(self, payload: dict) -> httpx.Response:
        params = {"key": self.webhook_key or ""}
        return httpx.post(
            self.webhook_url,
            params=params,
            json=payload,
            timeout=15,
            headers={"Content-Type": "application/json"},
            follow_redirects=True,
        )

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
    def append_feedback(self, record: FeedbackRecord) -> None:
        payload = {
            "type": "feedback",
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
            # Row mapped to the sheet headers (A-I).
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
                record.submitted_at.isoformat(),
            ],
            # Explicit column names to help Apps Script map the row.
            "columns": [
                "Вакансия",
                "Нанимающий менеджер",
                "Рекомендации по улучшению работы рекрутера",
                "Рекрутер",
                "Общая оценка работы рекрутера? (1-5)",
                "Как оцениваете коммуникацию с рекрутером? (1-5)",
                "Вакансия закрыта в комфортные сроки? (1-5)",
                "Насколько релевантны кандидаты? (1-5)",
                "Как оцениваете качество процесса? (1-5)",
                "Дата и время",
            ],
            # Duplicate row under a generic key in case the script expects it.
            "values": [
                record.vacancy_title or record.vacancy_id,
                record.hiring_manager_full_name,
                record.recommendations or record.feedback_comment,
                record.recruiter_name,
                record.overall_rating,
                record.comms_rating,
                record.timeliness_rating,
                record.relevance_rating,
                record.process_quality_rating,
                record.submitted_at.isoformat(),
            ],
        }

        resp = self._post(payload)
        if resp.status_code >= 400:
            logger.error("Sheet webhook failed (%s): %s", resp.status_code, resp.text)
            resp.raise_for_status()
        logger.info("Sent feedback to sheet webhook for vacancy %s (status %s)", record.vacancy_id, resp.status_code)

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
    def upsert_user(self, user: User) -> None:
        payload = {
            "type": "user_upsert",
            # Spreadsheet columns (Users sheet)
            "tg_username": user.username or "",
            "name": user.full_name,
            "role_is_active": user.title or "",
            "mail": user.contact or "",
            # Extra field for lookup/debug
            "telegram_user_id": user.telegram_id,
        }
        resp = self._post(payload)
        if resp.status_code >= 400:
            logger.error("Sheet user upsert failed (%s): %s", resp.status_code, resp.text)
            resp.raise_for_status()
        logger.info("Upserted user %s into sheet webhook (status %s)", user.telegram_id, resp.status_code)

    def get_user(self, telegram_user_id: int, tg_username: Optional[str] = None) -> Optional[User]:
        payload = {
            "type": "user_lookup",
            "telegram_user_id": telegram_user_id,
            "tg_username": tg_username or "",
        }
        resp = self._post(payload)
        if resp.status_code >= 400:
            logger.error("Sheet user lookup failed (%s): %s", resp.status_code, resp.text)
            resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("Sheet user lookup returned non-JSON response.")
            return None
        if not data or not data.get("found"):
            return None
        u = data.get("user") or {}
        return User(
            telegram_id=int(u.get("telegram_user_id") or telegram_user_id),
            full_name=u.get("name") or u.get("full_name") or "",
            username=u.get("tg_username") or u.get("tg_user_id") or u.get("username") or None,
            title=u.get("role_is_active") or u.get("title") or None,
            contact=u.get("mail") or u.get("contact") or None,
            permission_level=u.get("permission_level") or "hiring_manager",
            status=u.get("status") or "active",
        )

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
    def upsert_vacancy(self, vacancy: VacancyAssignment) -> None:
        payload = {
            "type": "vacancy_upsert",
            "vacancy_id": vacancy.vacancy_id,
            "vacancy_title": vacancy.vacancy_title,
            "recruiter_name": vacancy.recruiter_name,
            "hiring_manager_ids": vacancy.hiring_manager_ids,
        }
        resp = self._post(payload)
        if resp.status_code >= 400:
            logger.error("Sheet vacancy upsert failed (%s): %s", resp.status_code, resp.text)
            resp.raise_for_status()
        logger.info("Upserted vacancy %s into sheet webhook (status %s)", vacancy.vacancy_id, resp.status_code)

    def get_vacancy(self, vacancy_id: str) -> Optional[VacancyAssignment]:
        payload = {"type": "vacancy_lookup", "vacancy_id": vacancy_id}
        resp = self._post(payload)
        if resp.status_code >= 400:
            logger.error("Sheet vacancy lookup failed (%s): %s", resp.status_code, resp.text)
            resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("Sheet vacancy lookup returned non-JSON response.")
            return None
        if not data or not data.get("found"):
            return None
        v = data.get("vacancy") or {}
        return VacancyAssignment(
            vacancy_id=v.get("vacancy_id") or vacancy_id,
            vacancy_title=v.get("vacancy_title") or "",
            recruiter_name=v.get("recruiter_name") or "",
            hiring_manager_ids=list(v.get("hiring_manager_ids") or []),
        )


class GoogleSheetClient(SheetWebhookClient):
    """
    Backward-compatible client name. Uses the Apps Script webhook under the hood.
    """
