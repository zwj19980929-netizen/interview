import asyncio
import hashlib
import json
import math
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.ids import new_id
from app.core.prompt.contracts import prompt_contract
from app.core.time import utc_now
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest
from app.persistence.interface import new_work_item


@dataclass(frozen=True)
class ResumeEvidenceChunk:
    """One complete, bounded source slice used only to extract evidence."""

    chunk_id: str
    page_start: int
    page_end: int
    text: str
    estimated_tokens: int

    @property
    def source_pages(self) -> List[int]:
        return list(range(self.page_start, self.page_end + 1))


@dataclass(frozen=True)
class ResumeReviewPipelineResult:
    data: Dict[str, Any]
    processing: Dict[str, Any]


def review_input_hash(resume: Dict[str, Any], position: Dict[str, Any], role: Dict[str, Any]) -> str:
    value = "%s:%s:%s:%s" % (
        resume["file_hash"],
        position["id"],
        position["version"],
        role["version"],
    )
    return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


def review_work_idempotency_key(resume_document_id: str, input_hash: str) -> str:
    """Scope durable execution deduplication to one immutable resume version."""
    return "resume.review:%s:%s" % (resume_document_id, input_hash)


def _enqueue_review_work(
    transaction: Any,
    *,
    review: Dict[str, Any],
    resume_document_id: str,
    input_hash: str,
    organization_id: str,
) -> Dict[str, Any]:
    work = transaction.outbox.enqueue(
        new_work_item(
            organization_id=organization_id,
            kind="resume.review",
            aggregate_id=review["id"],
            idempotency_key=review_work_idempotency_key(resume_document_id, input_hash),
            payload={"resume_review_id": review["id"]},
        )
    )
    if work.get("aggregate_id") != review["id"]:
        raise RuntimeError("Resume review work idempotency resolved to another review.")
    return work


def queue_resume_review(
    transaction: Any,
    *,
    candidate_id: str,
    resume: Dict[str, Any],
    position: Dict[str, Any],
    role: Dict[str, Any],
    organization_id: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Idempotently create the review aggregate and its durable work item."""
    input_hash = review_input_hash(resume, position, role)
    existing = next(
        (
            item
            for item in transaction.resume_reviews.list()
            if item["resume_document_id"] == resume["id"]
            and item["job_position_id"] == position["id"]
            and item["role_requirement_id"] == role["id"]
            and item["input_hash"] == input_hash
        ),
        None,
    )
    if existing:
        work = next(
            (
                item
                for item in transaction.outbox.list()
                if item.get("kind") == "resume.review" and item.get("aggregate_id") == existing["id"]
            ),
            None,
        )
        if work is None and existing.get("status") == "queued":
            work = _enqueue_review_work(
                transaction,
                review=existing,
                resume_document_id=resume["id"],
                input_hash=input_hash,
                organization_id=organization_id,
            )
        return existing, work

    now = utc_now()
    review = transaction.resume_reviews.add(
        {
            "id": new_id("resume_review"),
            "organization_id": organization_id,
            "candidate_profile_id": candidate_id,
            "resume_document_id": resume["id"],
            "job_position_id": position["id"],
            "role_requirement_id": role["id"],
            "input_hash": input_hash,
            "status": "queued",
            "processing_stage": "queued",
            "processing_progress": {"completed_chunks": 0, "total_chunks": None},
            "processing_strategy": None,
            "evidence_chunks": [],
            "project_evidence": [],
            "skill_evidence": [],
            "warnings": [],
            "summary": None,
            "screening_recommendation": None,
            "screening_score": None,
            "screening_summary": None,
            "matched_requirements": [],
            "unmet_requirements": [],
            "human_review_status": "pending",
            "human_decision": None,
            "human_review_note": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    work = _enqueue_review_work(
        transaction,
        review=review,
        resume_document_id=resume["id"],
        input_hash=input_hash,
        organization_id=organization_id,
    )
    return review, work


class ResumeReviewPipeline:
    """Hides redaction, context budgets, Map/Reduce, and evidence provenance."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway
        self.single_pass_budget = _positive_env("INTERVIEWER_RESUME_SINGLE_PASS_TOKENS", 8000)
        self.chunk_budget = _positive_env("INTERVIEWER_RESUME_CHUNK_TOKENS", 3500)
        self.reduce_budget = _positive_env("INTERVIEWER_RESUME_REDUCE_TOKENS", 8000)
        self.map_concurrency = _positive_env("INTERVIEWER_RESUME_MAP_CONCURRENCY", 3)
        self.max_compaction_rounds = _positive_env("INTERVIEWER_RESUME_COMPACTION_ROUNDS", 4)
        self.single_pass_output_tokens = _positive_env(
            "INTERVIEWER_RESUME_SINGLE_PASS_OUTPUT_TOKENS", 6000
        )
        self.map_output_tokens = _positive_env("INTERVIEWER_RESUME_MAP_OUTPUT_TOKENS", 4000)
        self.compaction_output_tokens = _positive_env(
            "INTERVIEWER_RESUME_COMPACTION_OUTPUT_TOKENS", 4000
        )
        self.reduce_output_tokens = _positive_env("INTERVIEWER_RESUME_REDUCE_OUTPUT_TOKENS", 6000)

    async def process(
        self,
        *,
        parsed_text: str,
        page_count: int,
        position: Dict[str, Any],
        role: Dict[str, Any],
        organization_id: str,
        on_progress: Any = None,
    ) -> ResumeReviewPipelineResult:
        sanitized = redact_resume(parsed_text)
        estimated_tokens = estimate_tokens(sanitized)
        if estimated_tokens <= self.single_pass_budget:
            response, contract_version = await self._single_pass(
                sanitized=sanitized,
                position=position,
                role=role,
                organization_id=organization_id,
            )
            data = deepcopy(response.data)
            _attach_default_pages(data, list(range(1, max(1, page_count) + 1)))
            return ResumeReviewPipelineResult(
                data=data,
                processing={
                    "strategy": "single_pass",
                    "estimated_input_tokens": estimated_tokens,
                    "chunk_count": 1,
                    "chunks": [],
                    "prompt_versions": [contract_version],
                    "providers": [response.provider.model_dump()],
                    "usage": response.usage.model_dump(),
                },
            )

        chunks, page_structure = build_evidence_chunks(
            sanitized,
            page_count=max(1, page_count),
            token_budget=self.chunk_budget,
        )
        if on_progress:
            on_progress("extracting_evidence", 0, len(chunks), "map_reduce")
        evidence, chunk_records, prompt_versions, providers, usage = await self._map_chunks(
            chunks,
            position=position,
            role=role,
            organization_id=organization_id,
            on_progress=on_progress,
        )
        evidence, compact_versions, compact_providers, compact_usage = await self._fit_reduce_budget(
            evidence,
            role=role,
            organization_id=organization_id,
        )
        prompt_versions.extend(compact_versions)
        providers.extend(compact_providers)
        _add_usage(usage, compact_usage)
        if on_progress:
            on_progress("aggregating_review", len(chunks), len(chunks), "map_reduce")
        response, reduce_version = await self._reduce(
            evidence=evidence,
            position=position,
            role=role,
            organization_id=organization_id,
        )
        prompt_versions.append(reduce_version)
        providers.append(response.provider.model_dump())
        _add_usage(usage, response.usage.model_dump())
        data = deepcopy(response.data)
        _backfill_source_pages(data, evidence)
        if page_structure == "legacy_flat_text":
            data.setdefault("warnings", []).append(
                "该简历由旧版解析器生成，分块覆盖完整正文，但无法精确恢复逐页来源。"
            )
        return ResumeReviewPipelineResult(
            data=data,
            processing={
                "strategy": "map_reduce",
                "estimated_input_tokens": estimated_tokens,
                "chunk_count": len(chunks),
                "chunks": chunk_records,
                "page_structure": page_structure,
                "prompt_versions": list(dict.fromkeys(prompt_versions)),
                "providers": providers,
                "usage": usage,
            },
        )

    async def _single_pass(
        self,
        *,
        sanitized: str,
        position: Dict[str, Any],
        role: Dict[str, Any],
        organization_id: str,
    ) -> Tuple[Any, str]:
        contract = prompt_contract(
            "resume_review",
            {
                "position_name": position["name"],
                "role_description": role["description"],
                "sanitized_resume": sanitized,
            },
        )
        response = await self.gateway.invoke(
            cap.LLM_CHAT_JSON,
            ChatJSONRequest(
                organization_id=organization_id,
                purpose="resume_review",
                messages=contract.messages,
                json_schema=contract.response_schema,
                max_output_tokens=self.single_pass_output_tokens,
                metadata={
                    "resume_review_phase": "single_pass",
                    "resume_text": sanitized,
                    "position_name": position["name"],
                    "must_have_skills": role.get("must_have_skills", []),
                    "prompt_version": contract.version,
                },
            ),
        )
        return response, contract.version

    async def _map_chunks(
        self,
        chunks: Sequence[ResumeEvidenceChunk],
        *,
        position: Dict[str, Any],
        role: Dict[str, Any],
        organization_id: str,
        on_progress: Any,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str], List[Dict[str, Any]], Dict[str, int]]:
        semaphore = asyncio.Semaphore(self.map_concurrency)
        completed = 0
        lock = asyncio.Lock()

        async def run(chunk: ResumeEvidenceChunk) -> Tuple[ResumeEvidenceChunk, Any, str]:
            nonlocal completed
            contract = prompt_contract(
                "resume_evidence_map",
                {
                    "position_name": position["name"],
                    "role_description": role["description"],
                    "must_have_skills": role.get("must_have_skills", []),
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "chunk_text": chunk.text,
                },
            )
            async with semaphore:
                response = await self.gateway.invoke(
                    cap.LLM_CHAT_JSON,
                    ChatJSONRequest(
                        organization_id=organization_id,
                        purpose="resume_review",
                        messages=contract.messages,
                        json_schema=contract.response_schema,
                        max_output_tokens=self.map_output_tokens,
                        metadata={
                            "resume_review_phase": "evidence_map",
                            "chunk_id": chunk.chunk_id,
                            "chunk_text": chunk.text,
                            "page_start": chunk.page_start,
                            "page_end": chunk.page_end,
                            "position_name": position["name"],
                            "must_have_skills": role.get("must_have_skills", []),
                            "prompt_version": contract.version,
                        },
                    ),
                )
            async with lock:
                completed += 1
                if on_progress:
                    on_progress("extracting_evidence", completed, len(chunks), "map_reduce")
            return chunk, response, contract.version

        results = await asyncio.gather(*(run(chunk) for chunk in chunks))
        combined: Dict[str, Any] = {"project_evidence": [], "skill_evidence": [], "warnings": []}
        records: List[Dict[str, Any]] = []
        versions: List[str] = []
        providers: List[Dict[str, Any]] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for chunk, response, version in results:
            for field in ("project_evidence", "skill_evidence"):
                for item in response.data.get(field, []):
                    combined[field].append({**deepcopy(item), "source_pages": chunk.source_pages})
            combined["warnings"].extend(response.data.get("warnings", []))
            records.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "estimated_tokens": chunk.estimated_tokens,
                    "status": "completed",
                    "evidence_count": len(response.data.get("project_evidence", []))
                    + len(response.data.get("skill_evidence", [])),
                    "prompt_version": version,
                    "provider": response.provider.model_dump(),
                }
            )
            versions.append(version)
            providers.append(response.provider.model_dump())
            _add_usage(usage, response.usage.model_dump())
        return _deduplicate_evidence(combined), records, versions, providers, usage

    async def _fit_reduce_budget(
        self,
        evidence: Dict[str, Any],
        *,
        role: Dict[str, Any],
        organization_id: str,
    ) -> Tuple[Dict[str, Any], List[str], List[Dict[str, Any]], Dict[str, int]]:
        versions: List[str] = []
        providers: List[Dict[str, Any]] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        current = evidence
        for _ in range(self.max_compaction_rounds + 1):
            serialized = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            if estimate_tokens(serialized) <= self.reduce_budget:
                return current, versions, providers, usage
            groups = _evidence_groups(current, self.reduce_budget)
            if len(groups) <= 1:
                raise ProviderError(
                    "resume_evidence_budget_exceeded",
                    "Resume evidence cannot fit the configured aggregation context budget.",
                    retryable=False,
                    details={"estimated_tokens": estimate_tokens(serialized), "budget": self.reduce_budget},
                )
            compacted: Dict[str, Any] = {"project_evidence": [], "skill_evidence": [], "warnings": []}
            for group in groups:
                contract = prompt_contract(
                    "resume_evidence_compaction",
                    {
                        "role_description": role["description"],
                        "must_have_skills": role.get("must_have_skills", []),
                        "evidence_json": json.dumps(group, ensure_ascii=False, separators=(",", ":")),
                    },
                )
                response = await self.gateway.invoke(
                    cap.LLM_CHAT_JSON,
                    ChatJSONRequest(
                        organization_id=organization_id,
                        purpose="resume_review",
                        messages=contract.messages,
                        json_schema=contract.response_schema,
                        max_output_tokens=self.compaction_output_tokens,
                        metadata={
                            "resume_review_phase": "evidence_compaction",
                            "evidence": group,
                            "must_have_skills": role.get("must_have_skills", []),
                            "prompt_version": contract.version,
                        },
                    ),
                )
                for field in compacted:
                    compacted[field].extend(deepcopy(response.data.get(field, [])))
                versions.append(contract.version)
                providers.append(response.provider.model_dump())
                _add_usage(usage, response.usage.model_dump())
            current = _deduplicate_evidence(compacted)
        raise ProviderError(
            "resume_evidence_budget_exceeded",
            "Resume evidence remains larger than the aggregation context budget after compaction.",
            retryable=False,
        )

    async def _reduce(
        self,
        *,
        evidence: Dict[str, Any],
        position: Dict[str, Any],
        role: Dict[str, Any],
        organization_id: str,
    ) -> Tuple[Any, str]:
        evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        contract = prompt_contract(
            "resume_review_reduce",
            {
                "position_name": position["name"],
                "role_description": role["description"],
                "must_have_skills": role.get("must_have_skills", []),
                "evidence_json": evidence_json,
            },
        )
        response = await self.gateway.invoke(
            cap.LLM_CHAT_JSON,
            ChatJSONRequest(
                organization_id=organization_id,
                purpose="resume_review",
                messages=contract.messages,
                json_schema=contract.response_schema,
                max_output_tokens=self.reduce_output_tokens,
                metadata={
                    "resume_review_phase": "final_reduce",
                    "evidence": evidence,
                    "position_name": position["name"],
                    "must_have_skills": role.get("must_have_skills", []),
                    "prompt_version": contract.version,
                },
            ),
        )
        return response, contract.version


def redact_resume(text: str) -> str:
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL_REDACTED]", text)
    value = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1\d{10}(?!\d)", "[PHONE_REDACTED]", value)
    return re.sub(r"(?im)^(性别|年龄|婚姻状况|照片)\s*[:：].*$", "", value).strip()


def estimate_tokens(text: str) -> int:
    units = 0
    for character in text:
        if character.isspace():
            continue
        units += 4 if _is_cjk(character) else 1
    return max(1, math.ceil(units / 4)) if text else 0


def build_evidence_chunks(
    text: str,
    *,
    page_count: int,
    token_budget: int,
) -> Tuple[List[ResumeEvidenceChunk], str]:
    has_page_boundaries = "\f" in text
    raw_pages = text.split("\f") if has_page_boundaries else [text]
    if has_page_boundaries:
        pages = [(index, value.strip()) for index, value in enumerate(raw_pages, start=1) if value.strip()]
        structure = "page_delimited"
    else:
        pages = [(1, text.strip())]
        structure = "legacy_flat_text" if page_count > 1 else "single_page"
    pieces: List[Tuple[int, int, str]] = []
    for page_number, page_text in pages:
        if estimate_tokens(page_text) <= token_budget:
            pieces.append((page_number, page_number, page_text))
            continue
        pieces.extend((page_number, page_number, part) for part in _split_text_by_budget(page_text, token_budget))

    chunks: List[ResumeEvidenceChunk] = []
    pending: List[str] = []
    pending_start = 1
    pending_end = 1
    for page_start, page_end, piece in pieces:
        candidate = "\n\n".join([*pending, piece])
        if pending and estimate_tokens(candidate) > token_budget:
            combined = "\n\n".join(pending)
            chunks.append(
                ResumeEvidenceChunk(
                    chunk_id="chunk_%03d" % (len(chunks) + 1),
                    page_start=pending_start,
                    page_end=pending_end,
                    text=combined,
                    estimated_tokens=estimate_tokens(combined),
                )
            )
            pending = [piece]
            pending_start = page_start
            pending_end = page_end
        else:
            if not pending:
                pending_start = page_start
            pending.append(piece)
            pending_end = page_end
    if pending:
        combined = "\n\n".join(pending)
        chunks.append(
            ResumeEvidenceChunk(
                chunk_id="chunk_%03d" % (len(chunks) + 1),
                page_start=pending_start,
                page_end=pending_end,
                text=combined,
                estimated_tokens=estimate_tokens(combined),
            )
        )
    if not chunks:
        raise ProviderError("resume_text_empty", "Parsed resume text is empty.", retryable=False)
    if any(chunk.estimated_tokens > token_budget for chunk in chunks):
        raise RuntimeError("Resume chunk builder exceeded its token budget.")
    return chunks, structure


def _split_text_by_budget(text: str, token_budget: int) -> List[str]:
    paragraphs = [value for value in re.split(r"(?<=\n)", text) if value]
    result: List[str] = []
    pending = ""
    for paragraph in paragraphs:
        candidate = pending + paragraph
        if pending and estimate_tokens(candidate) > token_budget:
            result.append(pending.strip())
            pending = ""
        if estimate_tokens(paragraph) <= token_budget:
            pending += paragraph
            continue
        if pending:
            result.append(pending.strip())
            pending = ""
        result.extend(_split_charwise(paragraph, token_budget))
    if pending.strip():
        result.append(pending.strip())
    return [value for value in result if value]


def _split_charwise(text: str, token_budget: int) -> List[str]:
    result: List[str] = []
    start = 0
    units = 0
    limit = token_budget * 4
    for index, character in enumerate(text):
        units += 0 if character.isspace() else (4 if _is_cjk(character) else 1)
        if units > limit and index > start:
            result.append(text[start:index].strip())
            start = index
            units = 0 if character.isspace() else (4 if _is_cjk(character) else 1)
    if text[start:].strip():
        result.append(text[start:].strip())
    return result


def _evidence_groups(evidence: Dict[str, Any], budget: int) -> List[Dict[str, Any]]:
    entries = [
        (field, item)
        for field in ("project_evidence", "skill_evidence")
        for item in evidence.get(field, [])
    ]
    warning_entries = [("warnings", item) for item in evidence.get("warnings", [])]
    groups: List[Dict[str, Any]] = []
    current = {"project_evidence": [], "skill_evidence": [], "warnings": []}
    target = max(256, int(budget * 0.8))
    for field, item in [*entries, *warning_entries]:
        candidate = deepcopy(current)
        candidate[field].append(deepcopy(item))
        current_count = sum(len(current[value]) for value in current)
        if any(current.values()) and (
            estimate_tokens(json.dumps(candidate, ensure_ascii=False)) > target or current_count >= 20
        ):
            groups.append(current)
            current = {"project_evidence": [], "skill_evidence": [], "warnings": []}
        current[field].append(deepcopy(item))
        if estimate_tokens(json.dumps(current, ensure_ascii=False)) > budget:
            raise ProviderError(
                "resume_evidence_item_too_large",
                "One extracted evidence item exceeds the aggregation context budget.",
                retryable=False,
            )
    if any(current.values()):
        groups.append(current)
    return groups


def _deduplicate_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"project_evidence": [], "skill_evidence": [], "warnings": []}
    for field in ("project_evidence", "skill_evidence"):
        seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for item in evidence.get(field, []):
            key = (str(item.get("label", "")).strip().lower(), str(item.get("evidence", "")).strip().lower())
            if key in seen:
                pages = sorted(set(seen[key].get("source_pages", [])) | set(item.get("source_pages", [])))
                seen[key]["source_pages"] = pages
            else:
                stored = deepcopy(item)
                stored["source_pages"] = sorted(set(stored.get("source_pages", [])))
                seen[key] = stored
        result[field] = list(seen.values())
    result["warnings"] = list(dict.fromkeys(str(item) for item in evidence.get("warnings", []) if str(item).strip()))
    return result


def _attach_default_pages(data: Dict[str, Any], pages: List[int]) -> None:
    for field in ("project_evidence", "skill_evidence"):
        for item in data.get(field, []):
            item.setdefault("source_pages", pages)
    for item in data.get("screening", {}).get("matched_requirements", []):
        item.setdefault("source_pages", pages)


def _backfill_source_pages(data: Dict[str, Any], evidence: Dict[str, Any]) -> None:
    all_evidence = [*evidence.get("project_evidence", []), *evidence.get("skill_evidence", [])]
    all_pages = sorted({page for item in all_evidence for page in item.get("source_pages", [])})
    for field in ("project_evidence", "skill_evidence"):
        for item in data.get(field, []):
            if item.get("source_pages"):
                continue
            item["source_pages"] = _matching_pages(item, all_evidence) or all_pages
    for item in data.get("screening", {}).get("matched_requirements", []):
        if not item.get("source_pages"):
            item["source_pages"] = _matching_pages(item, all_evidence) or all_pages


def _matching_pages(item: Dict[str, Any], evidence: Sequence[Dict[str, Any]]) -> List[int]:
    needle = "%s %s" % (item.get("label") or item.get("requirement") or "", item.get("evidence") or "")
    tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9_+#.-]{2,}|[\u4e00-\u9fff]{2,}", needle)}
    pages = set()
    for source in evidence:
        haystack = "%s %s" % (source.get("label", ""), source.get("evidence", ""))
        if not tokens or any(token in haystack.lower() for token in tokens):
            pages.update(source.get("source_pages", []))
    return sorted(pages)


def _add_usage(total: Dict[str, int], value: Dict[str, Any]) -> None:
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        total[field] = int(total.get(field, 0)) + int(value.get(field, 0))


def _positive_env(name: str, default: int) -> int:
    return max(1, int(os.getenv(name, str(default))))


def _is_cjk(character: str) -> bool:
    return "\u3400" <= character <= "\u9fff" or "\uf900" <= character <= "\ufaff"
