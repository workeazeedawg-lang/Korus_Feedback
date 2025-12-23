from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class User:
    telegram_id: int
    full_name: str
    title: str | None = None
    contact: str | None = None
    permission_level: str = "hiring_manager"  # hiring_manager / admin / recruiter
    status: str = "active"


@dataclass
class VacancyAssignment:
    vacancy_id: str
    vacancy_title: str
    recruiter_name: str
    hiring_manager_ids: List[int] = field(default_factory=list)


@dataclass
class FeedbackRecord:
    vacancy_id: str
    vacancy_title: str
    recruiter_name: str
    hiring_manager_full_name: str
    telegram_user_id: int
    feedback_comment: str
    overall_rating: int
    comms_rating: int
    timeliness_rating: int
    relevance_rating: int
    process_quality_rating: int
    recommendations: str
    submitted_at: datetime
