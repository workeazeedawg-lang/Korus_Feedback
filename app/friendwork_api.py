from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import httpx


class FriendWorkClient:
    def __init__(self, base_url: str, token: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_job(self, job_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/jobs/{job_id}"
        resp = httpx.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        resp = httpx.post(url, headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _name_from_account(account: Dict[str, Any]) -> Optional[str]:
        if not account:
            return None
        first_raw = account.get("firstName")
        last_raw = account.get("lastName")
        first = str(first_raw).strip() if first_raw is not None else ""
        last = str(last_raw).strip() if last_raw is not None else ""
        full = " ".join([part for part in [last, first] if part])
        return full or None

    def extract_recruiter_name(self, job_data: Dict[str, Any]) -> Optional[str]:
        return self._name_from_account(job_data.get("responsibleAccount") or {})

    def extract_hiring_manager_names(self, job_data: Dict[str, Any]) -> List[str]:
        names: List[str] = []

        # Common shapes to try
        for key in ("hiringManager", "hiringManagers", "hiringManagerAccount", "hiringManagerUser"):
            value = job_data.get(key)
            if isinstance(value, dict):
                name = self._name_from_account(value)
                if name:
                    names.append(name)
            elif isinstance(value, list):
                for item in value:
                    name = self._name_from_account(item or {})
                    if name:
                        names.append(name)

        # Try team/participants arrays with roles
        for key in ("team", "participants", "members", "jobTeam"):
            items = job_data.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                role = (
                    item.get("role")
                    or item.get("position")
                    or item.get("functionTypeName")
                    or ""
                ).lower()
                if "нанимающ" in role or "hiring manager" in role:
                    name = self._name_from_account(item.get("account") or item)
                    if name:
                        names.append(name)

        # FriendWork uses teamMembers with functionTypeName
        team_members = job_data.get("teamMembers")
        if isinstance(team_members, dict):
            items = team_members.get("Items") or team_members.get("items")
            if isinstance(items, list):
                team_members = items
            else:
                team_members = [team_members]
        if isinstance(team_members, list):
            for item in team_members:
                role = (item.get("functionTypeName") or "").lower()
                if "нанимающ" in role or "hiring manager" in role:
                    name = self._name_from_account(item)
                    if name:
                        names.append(name)

        # Fallback: check customFieldValues for hiring manager fields only
        custom_fields = job_data.get("customFieldValues")
        if isinstance(custom_fields, list):
            for item in custom_fields:
                system_name = (item.get("SystemName") or item.get("systemName") or "").lower()
                value_raw = item.get("Value") or item.get("value")
                value = str(value_raw).strip() if value_raw is not None else ""
                if not value:
                    continue
                if "hiring" in system_name and "manager" in system_name and "interviewer" not in system_name:
                    names.append(value)

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for name in names:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(name)
        return unique

    @staticmethod
    def extract_candidate_count(job_data: Dict[str, Any]) -> Optional[int]:
        for key in (
            "candidateCount",
            "candidatesCount",
            "totalCandidates",
            "totalCandidateCount",
            "candidateCountAll",
        ):
            if key in job_data and job_data[key] is not None:
                try:
                    return int(job_data[key])
                except Exception:
                    return None
        return None

    @staticmethod
    def extract_tech_interview_count(job_data: Dict[str, Any]) -> Optional[int]:
        for key in (
            "techInterviewCount",
            "technicalInterviewCount",
            "techInterviewsCount",
        ):
            if key in job_data and job_data[key] is not None:
                try:
                    return int(job_data[key])
                except Exception:
                    return None
        return None

    def iter_candidate_histories(
        self,
        job_id: str,
        page_size: int = 200,
        statuses_ids: Optional[List[int]] = None,
        max_pages: int = 50,
    ) -> Iterable[Dict[str, Any]]:
        page = 1
        prev_page_marker = None
        while page <= max_pages:
            payload: Dict[str, Any] = {"JobId": int(job_id), "Page": page, "PageSize": page_size}
            if statuses_ids:
                payload["StatusesIds"] = statuses_ids
            data = self._post("/Candidate/CandidatesHistories", payload)
            items = (
                data.get("CandidateHistories")
                or data.get("candidateHistories")
                or data.get("Items")
                or []
            )
            if not items:
                break
            first_item = items[0] if isinstance(items, list) and items else {}
            page_marker = (
                first_item.get("CandidateHistoryId")
                or first_item.get("candidateHistoryId")
                or first_item.get("Id")
                or f"{first_item.get('Name')}-{first_item.get('Timestamp')}-{first_item.get('JobId')}"
            )
            if page_marker == prev_page_marker:
                break
            prev_page_marker = page_marker
            for item in items:
                yield item
            total = data.get("TotalCount") or data.get("totalCount")
            if total and page * page_size >= int(total):
                break
            if len(items) < page_size:
                break
            page += 1

    def count_candidates_in_job(
        self,
        job_id: str,
        status_name: Optional[str] = None,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> int:
        seen = set()
        target = status_name.strip().lower() if status_name else None
        for item in self.iter_candidate_histories(job_id, page_size=page_size, max_pages=max_pages):
            item_job_id = item.get("JobId") or item.get("jobId")
            # Only count items that explicitly match the vacancy job id.
            if item_job_id is None or str(item_job_id) != str(job_id):
                continue
            name = str(item.get("Name") or item.get("name") or "").strip().lower()
            if target and not (name == target or name.startswith(target)):
                continue
            candidate_id = (
                item.get("CandidateId")
                or item.get("candidateId")
                or item.get("CandidateID")
            )
            if candidate_id is None:
                candidate = item.get("Candidate") or item.get("candidate")
                if isinstance(candidate, dict):
                    candidate_id = candidate.get("Id") or candidate.get("id")
            if candidate_id is None:
                key = (
                    item.get("CandidateHistoryId")
                    or item.get("candidateHistoryId")
                    or f"{name}-{item.get('Timestamp')}-{item_job_id}"
                )
            else:
                key = candidate_id
            seen.add(str(key))
        return len(seen)
