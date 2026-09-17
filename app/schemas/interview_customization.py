from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


COMPANY_PROFILE_LABELS = {
    "company_name": "公司名称", "business_overview": "业务介绍",
    "products_services": "产品与服务", "additional_info": "其他资料",
}


def company_context_text(profile):
    return "\n\n".join(f"{label}：\n{profile[key].strip()}"
                      for key, label in COMPANY_PROFILE_LABELS.items() if profile.get(key, "").strip())


class CompanyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    company_name: Annotated[str, Field(max_length=12000)] = ""
    business_overview: Annotated[str, Field(max_length=12000)] = ""
    products_services: Annotated[str, Field(max_length=12000)] = ""
    additional_info: Annotated[str, Field(max_length=12000)] = ""

    @model_validator(mode="after")
    def bounded_profile(self):
        if sum(len(value) for value in self.model_dump().values()) > 12000:
            raise ValueError("企业资料合计不能超过 12000 字。")
        if len(company_context_text(self.model_dump())) > 12000:
            raise ValueError("企业资料连同栏目标题不能超过 12000 字。")
        return self


class InterviewCustomizationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_version: Annotated[int, Field(ge=0)]
    skill_instructions: Optional[Annotated[str, Field(max_length=6000)]] = None
    company_profile: Optional[CompanyProfile] = None

    @model_validator(mode="after")
    def require_module(self):
        supplied = self.model_fields_set - {"expected_version"}
        if not supplied or any(getattr(self, field) is None for field in supplied):
            raise ValueError("请提供 Skill 正文或企业资料；清空时使用空字符串或空对象。")
        return self


class CustomizationSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    skill_id: str
    name: str
    instructions: str
    revision: int
    status: str


class InterviewCustomizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int
    skill: Optional[CustomizationSkill]
    company_profile: CompanyProfile
    updated_at: Optional[str]
