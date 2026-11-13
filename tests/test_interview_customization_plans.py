"""Saved optional inputs reach new plans without changing reviewed snapshots."""
from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.schemas.api import InterviewPlanGenerateRequest
from app.services.interview_customization import InterviewCustomizationService
from app.services.interview_skills import InterviewSkillService
from app.services.plan_assembly import InterviewPlanAssembly
from test_optional_interview_context import assembly_fixture, request_body


@pytest.mark.anyio
@pytest.mark.parametrize("has_skill,has_company", [(False, False), (True, False), (False, True), (True, True)])
async def test_saved_modules_are_inherited_independently(has_skill, has_company):
    store, request = await assembly_fixture()
    service = InterviewCustomizationService(store)
    config = service.update({"expected_version": 0,
        "skill_instructions": "# 我的面试风格\n请用自然的短句交流。" if has_skill else "",
        "company_profile": {"business_overview": "为物流企业提供调度软件。"} if has_company else {},
    }, actor_id="author")
    plan = await InterviewPlanAssembly(store).assemble(request)
    assert plan["customization_version"] == config["version"]
    assert bool(plan.get("enterprise_skill_snapshot")) is has_skill
    assert bool(plan.get("company_context")) is has_company
    if has_skill:
        assert plan["enterprise_skill_snapshot"]["skill_id"] == config["skill"]["skill_id"]
    if has_company:
        assert plan["company_context"] == "业务介绍：\n为物流企业提供调度软件。"


@pytest.mark.anyio
async def test_explicit_overrides_and_opt_out_do_not_reintroduce_defaults(monkeypatch):
    store, request = await assembly_fixture()
    config = InterviewCustomizationService(store).update({"expected_version": 0,
        "skill_instructions": "先从候选人的经历聊起。", "company_profile": {"company_name": "默认公司"}}, actor_id="author")
    explicit = InterviewSkillService(store).create({"package": {"name": "本场风格", "instructions": "每次只问一个问题。"}}, actor_id="author")
    assembly = InterviewPlanAssembly(store)
    custom_company = await assembly.assemble(replace(request, company_context="本场公司资料"))
    assert custom_company["enterprise_skill_id"] == config["skill"]["skill_id"]
    assert custom_company["company_context"] == "本场公司资料"
    custom_skill = await assembly.assemble(replace(request, skill_id=explicit["id"]))
    assert custom_skill["enterprise_skill_id"] == explicit["id"]
    assert custom_skill["company_context"] == "公司名称：\n默认公司"

    def unexpected(*args, **kwargs):
        pytest.fail("An explicit opt-out or two explicit inputs must bypass default resolution")
    monkeypatch.setattr(InterviewCustomizationService, "resolve_defaults", unexpected)
    opted_out = await assembly.assemble(replace(request, use_customization_defaults=False))
    assert not opted_out.get("enterprise_skill_snapshot") and not opted_out.get("company_context")
    both = await assembly.assemble(replace(request, skill_id=explicit["id"], company_context="本场资料"))
    assert both["enterprise_skill_id"] == explicit["id"] and both["company_context"] == "本场资料"
    explicit_opt_out = await assembly.assemble(replace(request, use_customization_defaults=False,
        skill_id=explicit["id"], company_context="本场资料"))
    assert explicit_opt_out["enterprise_skill_snapshot"]["skill_id"] == explicit["id"]


@pytest.mark.anyio
async def test_defaults_are_frozen_before_model_work_and_old_plans_do_not_drift(monkeypatch):
    store, request = await assembly_fixture()
    service = InterviewCustomizationService(store)
    first = service.update({"expected_version": 0, "skill_instructions": "第一版交流方式。",
        "company_profile": {"business_overview": "原有业务介绍。"}}, actor_id="author")
    frozen = service.resolve_defaults()
    assembly = InterviewPlanAssembly(store)
    prepare = assembly._prepare_inquiry_units

    async def change_defaults_during_generation(*args, **kwargs):
        service.update({"expected_version": first["version"], "skill_instructions": "第二版交流方式。",
            "company_profile": {"business_overview": "更新后的业务介绍。"}}, actor_id="editor")
        return await prepare(*args, **kwargs)
    monkeypatch.setattr(assembly, "_prepare_inquiry_units", change_defaults_during_generation)
    old = await assembly.assemble(request)
    assert old["enterprise_skill_snapshot"] == frozen["skill_snapshot"]
    assert old["company_context"] == frozen["company_context"]
    assert old["customization_version"] == first["version"]
    original = deepcopy(old)
    latest = await InterviewPlanAssembly(store).assemble(request)
    assert latest["enterprise_skill_snapshot"]["revision_id"] != old["enterprise_skill_snapshot"]["revision_id"]
    assert latest["company_context"] == "业务介绍：\n更新后的业务介绍。"
    with assembly.persistence.transaction("org_default") as tx:
        assert tx.interview_plans.get(old["id"]) == original


@pytest.mark.anyio
@pytest.mark.parametrize("damaged_module", ["skill", "company"])
async def test_explicit_override_does_not_read_an_unused_damaged_default(damaged_module):
    store, request = await assembly_fixture()
    service = InterviewCustomizationService(store)
    config = service.update({"expected_version": 0, "skill_instructions": "已保存的风格。",
        "company_profile": {"business_overview": "已保存的企业介绍。"}}, actor_id="author")
    explicit = InterviewSkillService(store).create({"package": {"name": "本场风格", "instructions": "自然交流。"}}, actor_id="author")
    if damaged_module == "skill":
        revision = next(row for row in store.interview_skill_revisions.values() if row["skill_id"] == config["skill"]["skill_id"])
        revision["content_hash"] = "0" * 64
        request = replace(request, skill_id=explicit["id"])
    else:
        store.interview_customizations["org_default"]["content_hash"] = "0" * 64
        request = replace(request, company_context="本场企业资料")
    plan = await InterviewPlanAssembly(store).assemble(request)
    assert plan["enterprise_skill_id"] == (explicit["id"] if damaged_module == "skill" else config["skill"]["skill_id"])
    assert plan["company_context"] == ("业务介绍：\n已保存的企业介绍。" if damaged_module == "skill" else "本场企业资料")
    with pytest.raises(ApiError):
        service.resolve_defaults()


@pytest.mark.parametrize("value", ["false", 0, None, []])
def test_default_switch_is_strict_at_api(value):
    with pytest.raises(ValidationError):
        InterviewPlanGenerateRequest(**request_body(use_customization_defaults=value))


@pytest.mark.anyio
async def test_internal_default_switch_is_validated_before_resolving(monkeypatch):
    store, request = await assembly_fixture()
    monkeypatch.setattr(InterviewCustomizationService, "resolve_defaults", lambda *args: pytest.fail("invalid switch reached defaults"))
    with pytest.raises(ApiError) as error:
        await InterviewPlanAssembly(store).assemble(replace(request, use_customization_defaults="false"))
    assert error.value.code == "INTERVIEW_CUSTOMIZATION_POLICY_INVALID"
