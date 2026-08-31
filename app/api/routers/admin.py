from typing import Any, Dict, Optional

from fastapi import APIRouter, Header

from app.schemas.api import (
    ModelConfigurationCreate,
    ModelConfigurationPatch,
    ModelConfigurationTest,
    ModelRouteCreate,
    OutboxReplay,
    ProviderConnectionCreate,
    ProviderConnectionPatch,
    RetentionRun,
    ScoreCalibrationRun,
)
from app.transport.http.responses import ApiJSONResponse, collection_response
from app.transport.service_locator import services


router = APIRouter(default_response_class=ApiJSONResponse)

@router.get("/api/v1/admin/model-providers/catalog")
async def model_provider_catalog() -> Dict[str, Any]:
    return collection_response(services()["model_admin"].catalog())


@router.post("/api/v1/admin/model-provider-connections")
async def create_model_provider_connection(payload: ProviderConnectionCreate) -> Dict[str, Any]:
    return services()["model_admin"].create_provider_connection(payload.model_dump())


@router.get("/api/v1/admin/model-provider-connections")
async def list_model_provider_connections() -> Dict[str, Any]:
    return collection_response(services()["model_admin"].list_provider_connections())


@router.get("/api/v1/admin/model-provider-connections/{connection_id}")
async def get_model_provider_connection(connection_id: str) -> Dict[str, Any]:
    return services()["model_admin"].get_provider_connection(connection_id)


@router.patch("/api/v1/admin/model-provider-connections/{connection_id}")
async def patch_model_provider_connection(connection_id: str, payload: ProviderConnectionPatch) -> Dict[str, Any]:
    return services()["model_admin"].patch_provider_connection(connection_id, payload.model_dump(exclude_unset=True))


@router.delete("/api/v1/admin/model-provider-connections/{connection_id}")
async def delete_model_provider_connection(connection_id: str, expected_version: int) -> Dict[str, Any]:
    return services()["model_admin"].delete_provider_connection(connection_id, expected_version)


@router.post("/api/v1/admin/model-provider-connections/{connection_id}/validate")
async def validate_model_provider_connection(connection_id: str) -> Dict[str, Any]:
    return await services()["model_admin"].validate_provider_connection(connection_id)


@router.get("/api/v1/admin/model-provider-connections/{connection_id}/model-catalog")
async def model_catalog_for_connection(connection_id: str, model_type: Optional[str] = None) -> Dict[str, Any]:
    return services()["model_admin"].model_catalog(connection_id, model_type)


@router.post("/api/v1/admin/model-configurations")
async def create_model_configuration(payload: ModelConfigurationCreate) -> Dict[str, Any]:
    return services()["model_admin"].create_model_configuration(payload.model_dump())


@router.get("/api/v1/admin/model-configurations")
async def list_model_configurations() -> Dict[str, Any]:
    return collection_response(services()["model_admin"].list_model_configurations())


@router.get("/api/v1/admin/model-configurations/{configuration_id}")
async def get_model_configuration(configuration_id: str) -> Dict[str, Any]:
    return services()["model_admin"].get_model_configuration(configuration_id)


@router.get("/api/v1/admin/model-configurations/{configuration_id}/voices")
async def get_model_configuration_voices(configuration_id: str) -> Dict[str, Any]:
    return services()["model_admin"].voice_catalog(configuration_id)


@router.patch("/api/v1/admin/model-configurations/{configuration_id}")
async def patch_model_configuration(configuration_id: str, payload: ModelConfigurationPatch) -> Dict[str, Any]:
    return services()["model_admin"].patch_model_configuration(
        configuration_id, payload.model_dump(exclude_unset=True)
    )


@router.delete("/api/v1/admin/model-configurations/{configuration_id}")
async def delete_model_configuration(configuration_id: str, expected_version: int) -> Dict[str, Any]:
    return services()["model_admin"].delete_model_configuration(configuration_id, expected_version)


@router.post("/api/v1/admin/model-configurations/{configuration_id}/test")
async def test_model_configuration(
    configuration_id: str, payload: Optional[ModelConfigurationTest] = None
) -> Dict[str, Any]:
    return await services()["model_admin"].test_model_configuration(
        configuration_id, payload.model_dump(exclude_none=True) if payload else {}
    )


@router.post("/api/v1/admin/model-routes")
async def create_model_route(payload: ModelRouteCreate) -> Dict[str, Any]:
    return services()["model_admin"].create_route(payload.model_dump())


@router.get("/api/v1/admin/model-routes")
async def list_model_routes() -> Dict[str, Any]:
    return collection_response(services()["model_admin"].list_routes())


@router.post("/api/v1/admin/model-routes/{route_id}/test")
async def test_model_route(route_id: str) -> Dict[str, Any]:
    return await services()["model_admin"].test_route(route_id)


@router.get("/api/v1/admin/work-items")
async def list_work_items(status: Optional[str] = None) -> Dict[str, Any]:
    return services()["operations"].work_items(status)


@router.post("/api/v1/admin/work-items/{work_item_id}/replay")
async def replay_work_item(
    work_item_id: str,
    payload: OutboxReplay,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["operations"].replay(
        work_item_id, reason=payload.reason, actor_id=x_actor_id
    )


@router.get("/api/v1/admin/audit-events")
async def list_audit_events() -> Dict[str, Any]:
    return collection_response(services()["operations"].audit_events())


@router.get("/api/v1/admin/evaluations/question-selection-fairness")
async def evaluate_question_selection_fairness(
    job_position_id: str,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["fairness"].selection_distribution(
        job_position_id, actor_id=x_actor_id
    )


@router.post("/api/v1/admin/evaluations/score-calibration")
async def evaluate_score_calibration(
    payload: ScoreCalibrationRun,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["fairness"].score_calibration(
        payload.model_dump(), actor_id=x_actor_id
    )


@router.post("/api/v1/admin/session-monitor/run")
async def run_session_heartbeat_monitor() -> Dict[str, Any]:
    items = services()["session_monitor"].run_once()
    return {"items": items, "timed_out_count": len(items)}


@router.post("/api/v1/admin/retention/run")
async def run_retention(
    payload: RetentionRun,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["retention"].run(
        dry_run=payload.dry_run,
        actor_id=x_actor_id,
        now=payload.now,
    )
