from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.auth import Principal, _principal, _required_roles
from app.core.errors import ApiError, api_error_handler, persistence_error_handler, request_validation_error_handler
from app.persistence.errors import ConcurrencyConflict, PersistenceError, RecordAlreadyExists
from app.persistence.interface import InterviewCustomizationRepository
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.schemas.interview_customization import CompanyProfile, InterviewCustomizationUpdate, company_context_text
from app.services.interview_customization import InterviewCustomizationService
from app.services.interview_skills import InterviewSkillService


@pytest.fixture(params=["memory", "sqlite"])
def bundle(request, tmp_path):
    store = SQLiteStore(str(tmp_path / "customization.sqlite3")) if request.param == "sqlite" else InMemoryStore()
    return store, InterviewCustomizationService(store), InterviewSkillService(store)


def save(service, expected_version=0, organization_id="org_a", **fields):
    return service.update({"expected_version": expected_version, **fields}, actor_id="author", organization_id=organization_id)


def create_skill(service, organization_id="org_a", **fields):
    return service.create({"package": {"name": "原有访谈方法", "instructions": "先聊项目。", **fields}},
                          actor_id="author", organization_id=organization_id)


def test_absent_optional_defaults_have_no_writes_or_private_key_dependency(bundle, monkeypatch):
    store, service, _ = bundle
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.delenv("INTERVIEWER_CONTACT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("INTERVIEWER_CONTACT_LOOKUP_SECRET", raising=False)
    assert service.get(organization_id="org_a") == {
        "version": 0, "skill": None, "company_profile": CompanyProfile().model_dump(), "updated_at": None,
    }
    assert service.resolve_defaults("org_a") == {
        "skill_id": None, "skill_snapshot": None, "company_context": "", "customization_version": 0,
    }
    assert not store.interview_customizations and not store.audit_events


def test_two_independent_modules_preserve_each_other_and_freeze_existing_revision(bundle):
    _, service, skills = bundle
    first = save(service, skill_instructions="\n# 我的面试方法\n一次问一个重点。\n")
    assert first["skill"]["status"] == "active"
    assert first["skill"]["instructions"] == "\n# 我的面试方法\n一次问一个重点。\n"
    frozen = service.resolve_defaults("org_a")["skill_snapshot"]
    company = {"company_name": "合成企业", "business_overview": "为测试提供数据工具"}
    second = save(service, 1, company_profile=company)
    assert second["skill"] == first["skill"]
    assert second["version"] == 2 and second["updated_at"]
    resolved = service.resolve_defaults("org_a")
    assert resolved["company_context"] == "公司名称：\n合成企业\n\n业务介绍：\n为测试提供数据工具"
    assert resolved["skill_snapshot"] == frozen
    third = save(service, 2, skill_instructions="候选人说不会时自然换一个方向。")
    assert third["company_profile"] == second["company_profile"]
    assert third["skill"]["skill_id"] == first["skill"]["skill_id"]
    assert third["skill"]["revision"] == 2
    assert skills.load_compiled(frozen, "org_a")["content_hash"] == frozen["content_hash"]
    assert service.resolve_defaults("org_a")["skill_snapshot"]["revision"] == 2


def test_empty_company_profile_replaces_only_that_module(bundle):
    _, service, _ = bundle
    first = save(service, skill_instructions="先聊项目。", company_profile={"company_name": "测试公司"})
    cleared = save(service, 1, company_profile={})
    assert cleared["skill"] == first["skill"]
    assert cleared["company_profile"] == CompanyProfile().model_dump()
    assert service.resolve_defaults("org_a")["company_context"] == ""


def test_repeated_unchanged_save_does_not_consume_skill_revision_budget(bundle):
    store, service, _ = bundle
    first = save(service, skill_instructions="无需改动的访谈方法。")
    frozen = service.resolve_defaults("org_a")["skill_snapshot"]
    for version in range(1, 53):
        current = save(service, version, skill_instructions=first["skill"]["instructions"])
    assert current["version"] == 53
    assert current["skill"] == first["skill"]
    assert len(store.interview_skill_revisions) == 1
    assert service.resolve_defaults("org_a")["skill_snapshot"] == frozen


@pytest.mark.parametrize("state", ["retired", "revoked"])
def test_explicit_save_of_disabled_prose_creates_new_active_revision(bundle, state):
    _, service, skills = bundle
    first = save(service, skill_instructions="重新使用的访谈方法。")
    frozen = service.resolve_defaults("org_a")["skill_snapshot"]
    getattr(skills, "retire" if state == "retired" else "revoke")(
        first["skill"]["skill_id"], expected_version=1, actor_id="author", reason="停止使用", organization_id="org_a")
    updated = save(service, 1, skill_instructions=first["skill"]["instructions"])
    assert updated["skill"]["status"] == "active" and updated["skill"]["revision"] == 2
    assert service.resolve_defaults("org_a")["skill_snapshot"]["revision_id"] != frozen["revision_id"]
    if state == "revoked":
        with pytest.raises(ApiError):
            skills.load_compiled(frozen, "org_a")


def test_saved_empty_defaults_resolve_without_opening_private_keys(bundle, monkeypatch):
    store, service, _ = bundle
    save(service, skill_instructions="", company_profile={})
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.delenv("INTERVIEWER_CONTACT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("INTERVIEWER_CONTACT_LOOKUP_SECRET", raising=False)
    assert InterviewCustomizationService(store).resolve_defaults("org_a") == {
        "skill_id": None, "skill_snapshot": None, "company_context": "", "customization_version": 1,
    }


def test_existing_skills_are_only_a_fallback_until_defaults_are_explicit(bundle):
    store, service, skills = bundle
    first = create_skill(skills)
    second = create_skill(skills, instructions="最近保存的可用方法。")
    inactive = create_skill(skills, instructions="最近但已停用。")
    skills.retire(inactive["id"], expected_version=1, actor_id="author", reason="暂时不用", organization_id="org_a")
    # Existing timestamps have second precision; make recency explicit rather
    # than relying on three rapid writes landing in different wall-clock seconds.
    with service.persistence.transaction("org_a") as tx:
        for index, item in enumerate([first, second, inactive]):
            current = tx.interview_skills.get(item["id"])
            current["updated_at"] = f"2026-09-10T00:00:0{index}Z"
            tx.interview_skills.update(current, expected_version=current["version"])
    assert service.get(organization_id="org_a")["skill"]["skill_id"] == second["id"]
    assert service.resolve_defaults("org_a")["skill_id"] == second["id"]
    assert not store.interview_customizations
    bound = save(service, company_profile={"company_name": "合成测试"})
    assert bound["skill"]["skill_id"] == second["id"]
    cleared = save(service, 1, skill_instructions="  \n")
    assert cleared["skill"] is None
    create_skill(skills, instructions="以后添加的其它方法。")
    assert service.get(organization_id="org_a")["skill"] is None
    assert service.resolve_defaults("org_a")["skill_id"] is None
    assert skills.get(first["id"], organization_id="org_a")["status"] == "active"


@pytest.mark.parametrize("state", ["retired", "revoked"])
def test_disabled_default_is_skipped_without_falling_back_or_changing_its_history(bundle, state):
    _, service, skills = bundle
    item = create_skill(skills)
    save(service, company_profile={"company_name": "测试公司"})
    frozen = service.resolve_defaults("org_a")["skill_snapshot"]
    getattr(skills, "retire" if state == "retired" else "revoke")(
        item["id"], expected_version=1, actor_id="author", reason="停止使用", organization_id="org_a")
    create_skill(skills, instructions="不应自动替换默认方法。")
    resolved = service.resolve_defaults("org_a")
    assert resolved["skill_id"] is None and resolved["skill_snapshot"] is None
    assert "测试公司" in resolved["company_context"]
    assert service.get(organization_id="org_a")["skill"]["status"] == state
    if state == "retired":
        assert skills.load_compiled(frozen, "org_a")
    else:
        with pytest.raises(ApiError):
            skills.load_compiled(frozen, "org_a")


@pytest.mark.parametrize("allowed", [None, [], ["questions.read"]])
def test_editing_default_preserves_resources_and_explicit_tool_restrictions(bundle, allowed):
    _, service, skills = bundle
    resources = [{"id": "method", "title": "提问方法", "content": "保留这份用户材料。"}]
    item = create_skill(skills, resources=resources, allowed_tools=allowed)
    saved = save(service, skill_instructions="# 新正文\nhttps://example.org/ 只是文字。")
    current = skills.get(item["id"], organization_id="org_a")
    assert saved["skill"]["revision"] == 2
    assert saved["skill"]["name"] == "原有访谈方法"
    assert current["package"]["allowed_tools"] == allowed
    assert current["package"]["resources"] == resources
    assert current["schema_version"] == "interview_skill.v2"


def test_approved_legacy_skill_can_be_edited_as_v2_without_changing_old_snapshot(bundle):
    _, service, skills = bundle
    legacy = skills.create({"package": {"schema_version": "enterprise_interview_skill.v1", "name": "旧方法",
        "description": "旧记录", "instructions": "自然了解项目。", "allowed_tools": ["questions.read"]}},
        actor_id="author", organization_id="org_a")
    skills.validate(legacy["id"], expected_version=1, actor_id="author", organization_id="org_a")
    skills.approve(legacy["id"], expected_version=2, actor_id="author", reason="旧版本已审阅",
                   review_confirmed=True, organization_id="org_a")
    frozen = service.resolve_defaults("org_a")["skill_snapshot"]
    compiled = skills.load_compiled(frozen, "org_a")
    updated = save(service, skill_instructions="自由编写的新方法。")
    assert updated["skill"]["status"] == "active" and updated["skill"]["revision"] == 2
    assert skills.load_compiled(frozen, "org_a") == compiled


def test_conflicts_do_not_create_orphan_revisions_or_partial_configuration(bundle):
    store, service, _ = bundle
    save(service, skill_instructions="已保存的方法。", company_profile={"company_name": "原始公司"})
    before = deepcopy((store.interview_skill_revisions, store.interview_customizations, store.audit_events))
    with pytest.raises(ConcurrencyConflict):
        save(service, 0, skill_instructions="不得覆盖的编辑。", company_profile={"company_name": "不得保存"})
    assert (store.interview_skill_revisions, store.interview_customizations, store.audit_events) == before


def test_transaction_failure_after_skill_write_rolls_back_all_content_and_audits(bundle, monkeypatch):
    store, service, skills = bundle
    item = create_skill(skills)
    before = deepcopy((store.interview_skills, store.interview_skill_revisions, store.audit_events))
    monkeypatch.setattr(InterviewCustomizationRepository, "add", Mock(side_effect=RecordAlreadyExists("race")))
    with pytest.raises(ConcurrencyConflict):
        save(service, skill_instructions="不得留下孤立版本。")
    assert not store.interview_customizations
    assert (store.interview_skills, store.interview_skill_revisions, store.audit_events) == before
    assert skills.get(item["id"], organization_id="org_a")["revision"] == 1


def test_simultaneous_first_saves_commit_one_configuration_and_one_skill(bundle):
    store, service, _ = bundle
    def attempt(text):
        try:
            return save(service, skill_instructions=text)["skill"]["instructions"]
        except ConcurrencyConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["第一个编辑者的内容。", "第二个编辑者的内容。"]))
    assert results.count("conflict") == 1
    assert len(store.interview_customizations) == 1
    assert len(store.interview_skills) == 1 and len(store.interview_skill_revisions) == 1


def test_tenant_isolation_encryption_metadata_audit_and_no_plaintext_persistence(bundle):
    store, service, skills = bundle
    private_skill = "唯一的敏感访谈正文。"
    private_company = "唯一的公司内部业务说明。"
    saved = save(service, skill_instructions=private_skill, company_profile={"business_overview": private_company})
    assert service.get(organization_id="org_other")["version"] == 0
    assert service.resolve_defaults("org_other")["skill_id"] is None
    other = save(service, organization_id="org_other", company_profile={"company_name": "另一个组织"})
    assert other["skill"] is None
    assert service.get(organization_id="org_a")["skill"]["skill_id"] == saved["skill"]["skill_id"]
    raw = json.dumps({"config": store.interview_customizations,
        "revisions": store.interview_skill_revisions, "audit": store.audit_events}, ensure_ascii=False)
    assert private_skill not in raw and private_company not in raw
    assert "sealed_company_profile" not in json.dumps(saved)
    with service.persistence.transaction("org_other") as tx:
        assert tx.interview_customizations.get("org_a") is None
        with pytest.raises(ValueError):
            tx.interview_customizations.add({"id": "org_other_2", "organization_id": "org_other"})
    if isinstance(store, SQLiteStore):
        assert private_company.encode() not in store.path.read_bytes()


def test_company_ciphertext_cannot_be_transplanted_across_tenants(bundle):
    _, service, _ = bundle
    save(service, company_profile={"company_name": "组织A"})
    save(service, organization_id="org_b", company_profile={"company_name": "组织B"})
    with service.persistence.transaction("org_a") as tx:
        source = tx.interview_customizations.get("org_a")
    with service.persistence.transaction("org_b") as tx:
        current = tx.interview_customizations.get("org_b")
        current.update(sealed_company_profile=source["sealed_company_profile"], content_hash=source["content_hash"])
        tx.interview_customizations.update(current, expected_version=1)
    with pytest.raises(ApiError, match="面试定制资料无法读取"):
        service.get(organization_id="org_b")
    with pytest.raises(ApiError):
        service.resolve_defaults("org_b")


def test_default_resolution_does_not_read_or_authorize_excluded_modules(bundle, monkeypatch):
    _, service, _ = bundle
    save(service, skill_instructions="可用的默认方法。", company_profile={"company_name": "可用公司资料"})
    original_open = service._open_json
    monkeypatch.setattr(service, "_open_json", Mock(side_effect=AssertionError("unused company must not decrypt")))
    skill_only = service.resolve_defaults("org_a", include_company=False)
    assert skill_only["skill_snapshot"] and skill_only["company_context"] == ""
    assert skill_only["customization_version"] == 1
    monkeypatch.setattr(service, "_open_json", original_open)
    monkeypatch.setattr(service, "_selected_skill_id", Mock(side_effect=AssertionError("unused skill must not resolve")))
    company_only = service.resolve_defaults("org_a", include_skill=False)
    assert company_only["skill_id"] is None and company_only["skill_snapshot"] is None
    assert "可用公司资料" in company_only["company_context"]
    monkeypatch.setattr(service, "_open_json", Mock(side_effect=AssertionError("unused company must not decrypt")))
    assert service.resolve_defaults("org_a", include_skill=False, include_company=False) == {
        "skill_id": None, "skill_snapshot": None, "company_context": "", "customization_version": 1,
    }


def test_sqlite_reopen_restores_configuration_and_frozen_default(tmp_path):
    path = str(tmp_path / "reopen.sqlite3")
    service = InterviewCustomizationService(SQLiteStore(path))
    saved = save(service, skill_instructions="重启后保留的访谈方法。", company_profile={"products_services": "测试产品"})
    before = service.resolve_defaults("org_a")
    reopened = InterviewCustomizationService(SQLiteStore(path))
    assert reopened.get(organization_id="org_a") == saved
    assert reopened.resolve_defaults("org_a") == before


def test_sqlite_unique_organization_constraint_rejects_alternate_identity(tmp_path):
    store = SQLiteStore(str(tmp_path / "unique.sqlite3"))
    service = InterviewCustomizationService(store)
    save(service, company_profile={})
    duplicate = {**store.interview_customizations["org_a"], "id": "another"}
    with sqlite3.connect(str(store.path)) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO documents(collection,id,data) VALUES (?,?,?)",
            ("interview_customizations", "another", json.dumps(duplicate)))


@pytest.mark.parametrize("payload", [
    {}, {"expected_version": 0}, {"expected_version": True, "skill_instructions": "x"},
    {"expected_version": -1, "company_profile": {}}, {"expected_version": 0, "skill_instructions": None},
    {"expected_version": 0, "company_profile": None}, {"expected_version": 0, "skill_instructions": 1},
    {"expected_version": 0, "skill_instructions": "x" * 6001},
    {"expected_version": 0, "company_profile": {"company_name": 42}},
    {"expected_version": 0, "company_profile": {"company_name": "x" * 12001}},
    {"expected_version": 0, "company_profile": {"company_name": "x" * 6000, "business_overview": "y" * 6001}},
    {"expected_version": 0, "company_profile": {"company_name": "x" * 12000}},
    {"expected_version": 0, "company_profile": {"arbitrary": "x"}},
    {"expected_version": 0, "skill_instructions": "x", "organization_id": "org_victim"},
])
def test_strict_request_bounds_and_no_implicit_tenant_or_tool_fields(payload):
    with pytest.raises(ValidationError):
        InterviewCustomizationUpdate.model_validate(payload)


def test_profile_final_text_budget_and_blank_fields():
    prefix = len("公司名称：\n")
    profile = CompanyProfile(company_name="字" * (12000 - prefix))
    assert len(company_context_text(profile.model_dump())) == 12000
    assert company_context_text(CompanyProfile(company_name=" \n", additional_info="\t").model_dump()) == ""
    with pytest.raises(ValidationError, match="连同栏目标题"):
        CompanyProfile(company_name="字" * (12001 - prefix))


def test_http_roles_projection_partial_patch_and_conflict(monkeypatch):
    from app.api.routers import interview_customization as routes
    service = InterviewCustomizationService(InMemoryStore())
    monkeypatch.setattr(routes, "services", lambda: {"interview_customization": service})
    principal = Principal("author", "org_default", frozenset({"interviewer"}), True)
    monkeypatch.setattr(routes, "current_principal", lambda: principal)
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(PersistenceError, persistence_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get("/api/v1/interview-customization").json()["version"] == 0
        response = client.patch("/api/v1/interview-customization", json={"expected_version": 0, "skill_instructions": "自由正文"})
        assert response.status_code == 200
        assert set(response.json()) == {"version", "skill", "company_profile", "updated_at"}
        assert set(response.json()["skill"]) == {"skill_id", "name", "instructions", "revision", "status"}
        assert client.patch("/api/v1/interview-customization", json={"expected_version": 0, "company_profile": {}}).status_code == 409
        assert client.patch("/api/v1/interview-customization", json={"expected_version": 1, "company_profile": {}}).status_code == 200
        secret = "不应回显的正文" * 1000
        invalid = client.patch("/api/v1/interview-customization", json={"expected_version": 2, "company_profile": {"company_name": secret * 2}})
        assert invalid.status_code == 422 and secret not in invalid.text
        for roles in [frozenset({"reviewer"}), frozenset({"candidate"}), frozenset()]:
            principal = Principal("unauthorized", "org_default", roles, False)
            assert client.get("/api/v1/interview-customization").status_code == 403
            assert client.patch("/api/v1/interview-customization", json={"expected_version": 2, "company_profile": {}}).status_code == 403


def test_service_locator_binds_authenticated_organization():
    from app.transport.service_locator import ServiceLocator
    store = InMemoryStore()
    token = _principal.set(Principal("author", "org_bound", frozenset({"admin"}), True))
    try:
        locator = ServiceLocator(store)
        locator["interview_customization"].update({"expected_version": 0, "company_profile": {}}, actor_id="author")
        assert locator["interview_customization"].get()["version"] == 1
        assert set(store.interview_customizations) == {"org_bound"}
    finally:
        _principal.reset(token)
    assert _required_roles("/api/v1/interview-customization", "GET") == frozenset({"admin", "interviewer"})
    assert _required_roles("/api/v1/interview-customization", "PATCH") == frozenset({"admin", "interviewer"})


def test_postgresql_customization_migration_keeps_tenant_rls_and_constraints():
    from app.persistence.postgresql import MIGRATIONS
    migration = next(path for path in MIGRATIONS if path.name == "005_interview_customization.sql").read_text()
    assert "uq_interview_customization_organization" in migration
    assert "ck_interview_customization" in migration
    assert "id = organization_id" in migration
    assert "sealed_company_profile" in migration and "has_company_profile" in migration
    combined = "\n".join(Path(path).read_text() for path in MIGRATIONS)
    assert "ALTER TABLE documents FORCE ROW LEVEL SECURITY" in combined
    assert "WITH CHECK (organization_id = current_setting('app.organization_id', true))" in combined


@pytest.mark.skipif(not os.getenv("INTERVIEWER_TEST_POSTGRES_DSN"), reason="PostgreSQL runtime credentials are not configured")
def test_real_postgresql_customization_is_tenant_scoped_and_rejects_stale_saves():
    from app.repositories.postgresql import PostgreSQLStore
    service = InterviewCustomizationService(PostgreSQLStore(os.environ["INTERVIEWER_TEST_POSTGRES_DSN"]))
    org = "org_customization_" + uuid4().hex
    saved = save(service, organization_id=org, skill_instructions="合成数据库验收方法。", company_profile={"company_name": "合成企业"})
    assert saved["version"] == 1
    assert service.get(organization_id=org + "_other")["version"] == 0
    with pytest.raises(ConcurrencyConflict):
        save(service, organization_id=org, company_profile={})
    with service.persistence.transaction(org + "_other") as tx:
        assert tx._backend.get_document("interview_customizations", org) is None
    assert service.resolve_defaults(org)["skill_id"] == saved["skill"]["skill_id"]
