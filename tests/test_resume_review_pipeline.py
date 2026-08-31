import asyncio

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.services.resume_review import ResumeReviewPipeline


class _TruncatingSinglePassGateway:
    def __init__(self) -> None:
        self.phases: list[str] = []

    async def invoke(self, _capability, request):
        phase = request.metadata["resume_review_phase"]
        self.phases.append(phase)
        if phase == "single_pass":
            raise ProviderError(
                "provider_output_truncated",
                "Provider output reached the configured token limit before completing JSON.",
                retryable=False,
                details={"finish_reason": "length", "requested_max_output_tokens": 6000},
            )
        if phase == "evidence_map":
            data = {
                "project_evidence": [{"label": "订单平台", "evidence": "负责 Python 服务性能优化"}],
                "skill_evidence": [{"label": "Python", "evidence": "用于生产服务开发"}],
                "warnings": [],
            }
        elif phase == "final_reduce":
            data = {
                "summary": "具备相关生产开发证据。",
                "project_evidence": [
                    {"label": "订单平台", "evidence": "负责 Python 服务性能优化", "source_pages": [1]}
                ],
                "skill_evidence": [
                    {"label": "Python", "evidence": "用于生产服务开发", "source_pages": [1]}
                ],
                "warnings": [],
                "screening": {
                    "recommendation": "qualified",
                    "score": 80,
                    "summary": "核心要求有明确证据。",
                    "matched_requirements": [
                        {"requirement": "Python", "evidence": "生产服务开发", "source_pages": [1]}
                    ],
                    "unmet_requirements": [],
                },
            }
        else:  # pragma: no cover - makes an unexpected phase explicit
            raise AssertionError("Unexpected resume review phase: %s" % phase)
        return ChatJSONResponse(
            data=data,
            usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
            provider=ProviderMeta(
                provider_id="test",
                model="test-model",
                request_id="request-%s" % len(self.phases),
                latency_ms=1,
            ),
        )


def test_single_pass_output_truncation_falls_back_to_complete_map_reduce() -> None:
    gateway = _TruncatingSinglePassGateway()
    pipeline = ResumeReviewPipeline(gateway)  # type: ignore[arg-type]

    result = asyncio.run(
        pipeline.process(
            parsed_text="负责订单平台 Python 服务性能优化",
            page_count=1,
            position={"name": "后端工程师"},
            role={"description": "具备 Python 生产经验", "must_have_skills": ["python"]},
            organization_id="org_test",
        )
    )

    assert gateway.phases == ["single_pass", "evidence_map", "final_reduce"]
    assert result.processing["strategy"] == "map_reduce_after_output_truncation"
    assert result.processing["fallback_reason"] == "provider_output_truncated"
    assert result.processing["prompt_versions"] == [
        "resume_review.v6",
        "resume_evidence_map.v1",
        "resume_review_reduce.v4",
    ]
    assert result.data["screening"]["recommendation"] == "qualified"
    assert result.data["project_evidence"][0]["source_pages"] == [1]
