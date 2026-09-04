"""Production rollout and signed-acceptance gate for the real-time agent."""

from __future__ import annotations

import os
from typing import Any, Dict

from app.operations.interview_agent_acceptance import (
    load_and_validate_acceptance_report,
)


def realtime_agent_release_status(organization_id: str) -> Dict[str, Any]:
    """Return a secret-free, fail-closed deployment/tenant release decision."""

    production = (
        os.getenv("INTERVIEWER_RUNTIME_ENV", "development").strip().lower()
        == "production"
    )
    deployment_id = os.getenv("INTERVIEWER_DEPLOYMENT_ID", "").strip()
    release_revision = os.getenv("INTERVIEWER_RELEASE_REVISION", "").strip()
    configured_scope = bool(deployment_id and release_revision)
    enabled_organizations = {
        item.strip()
        for item in os.getenv(
            "INTERVIEWER_REALTIME_AGENT_ENABLED_ORGANIZATIONS", ""
        ).split(",")
        if item.strip()
    }
    organization_enabled = bool(
        not production
        or organization_id in enabled_organizations
    )
    acceptance_path = os.getenv("INTERVIEWER_AGENT_ACCEPTANCE_REPORT", "").strip()
    acceptance_secret = os.getenv(
        "INTERVIEWER_AGENT_ACCEPTANCE_REPORT_SECRET", ""
    )
    acceptance_ready = bool(
        not production
        or (
            configured_scope
            and acceptance_path
            and load_and_validate_acceptance_report(
                acceptance_path,
                acceptance_secret,
                expected_deployment_id=deployment_id,
                expected_release_revision=release_revision,
            )
        )
    )
    return {
        "production": production,
        "organization_id": organization_id,
        "organization_enabled": organization_enabled,
        "release_scope_configured": bool(not production or configured_scope),
        "acceptance_report_ready": acceptance_ready,
        "ready": bool(
            organization_enabled
            and (not production or configured_scope)
            and acceptance_ready
        ),
        "deployment_id": deployment_id or None,
        "release_revision": release_revision or None,
    }
