from __future__ import annotations

from typing import Any, Dict, List, Optional

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
