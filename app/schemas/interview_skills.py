from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.prompt.interview_skills import EnterpriseSkillPackage, FreeSkillPackage, NonBlank, register_package


class SkillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    package: Annotated[Union[FreeSkillPackage, EnterpriseSkillPackage], Field(discriminator="schema_version")]

    @field_validator("package", mode="before")
    @classmethod
    def register_skill_package(cls, value):
        return register_package(value)


class SkillRevise(SkillCreate):
    expected_version: Annotated[int, Field(ge=1)]


class SkillCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: Annotated[int, Field(ge=1)]
    revision_id: Optional[Annotated[NonBlank, Field(max_length=100)]] = None


class SkillLifecycleCommand(SkillCommand):
    reason: Annotated[NonBlank, Field(max_length=500)]


class SkillApprovalCommand(SkillLifecycleCommand):
    review_confirmed: Literal[True]
