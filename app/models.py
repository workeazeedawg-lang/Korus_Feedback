from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class User:
    telegram_id: int
    full_name: str
    username: str | None = None
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
    closed_date: str | None = None
    job_url: str | None = None
    candidate_count: int | None = None
    tech_interview_count: int | None = None


@dataclass
class FeedbackRecord:
    vacancy_id: str
    vacancy_title: str
    recruiter_name: str
    hiring_manager_full_name: str
    closed_date: str
    job_url: str
    candidate_count: int
    tech_interview_count: int
    telegram_user_id: int
    feedback_comment: str
    overall_rating: int
    comms_rating: int
    timeliness_rating: int
    relevance_rating: int
    process_quality_rating: int
    recommendations: str
    submitted_at: datetime
