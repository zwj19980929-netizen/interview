import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService


class SessionHeartbeatMonitor:
    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.interviews = InterviewService(store, persistence=self.persistence)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout_seconds = max(5, int(os.getenv("INTERVIEWER_HEARTBEAT_TIMEOUT_SECONDS", "45")))

    def run_once(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            sessions = [
                item
                for item in transaction.interview_sessions.list()
                if item.get("status") in {"waiting", "in_progress"}
            ]
        results: List[Dict[str, Any]] = []
        now = self.clock().astimezone(timezone.utc)
        for session in sessions:
            value = session.get("last_activity_at") or session.get("updated_at") or session.get("created_at")
            if not value:
                continue
            last_activity = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
            age = (now - last_activity).total_seconds()
            if age <= self.timeout_seconds:
                continue
            updated = self.interviews.timeout_interview(
                session["id"], "heartbeat_timeout", organization_id
            )
            results.append(
                {
                    "interview_id": session["id"],
                    "status": updated["status"],
                    "inactive_seconds": int(age),
                }
            )
        return results
