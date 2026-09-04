import asyncio
import base64
import struct

from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.provider import get_store, reset_store_for_tests
from app.workers.outbox import OutboxWorker
from app.file_storage.local import LocalPrivateFileAdapter
from app.file_storage.signing import FileAccessSigner
from app.model_gateway.schemas import ProviderMeta, TTSSynthesizeResponse
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.catalog import CatalogService


def test_batch_import_build_query_question_update_archive_and_regenerate() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post("/api/v1/job-positions", json={"code": "platform", "name": "Platform"}).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "Platform bank"},
    ).json()
    question = {
        "title": "Transactions",
        "question_text": "Explain transactions",
        "standard_answer": "Atomic durable changes",
        "key_points": ["atomicity"],
        "skills": ["database"],
        "rubric": {"semantic": 1},
    }
    queued = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/imports",
        json={"questions": [question]},
        headers={"Idempotency-Key": "import-1"},
    )
    assert queued.status_code == 202, queued.text
    duplicate = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/imports",
        json={"questions": [question]},
        headers={"Idempotency-Key": "import-1"},
    )
    assert duplicate.json()["job_id"] == queued.json()["job_id"]
    result = asyncio.run(OutboxWorker(get_store()).run_once())
    assert result[0]["kind"] == "knowledge_base.import"
    assert result[0]["status"] == "completed"
    build = api.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/builds/{queued.json()['job_id']}"
    )
    assert build.json()["status"] == "completed"
    questions = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions").json()["items"]
    assert len(questions) == 1
    current = questions[0]
    patched = api.patch(
        f"/api/v1/questions/{current['id']}",
        json={"expected_version": current["version"], "question_text": "Explain ACID transactions"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["speech_status"] == "pending"
    regenerated = api.post(
        f"/api/v1/questions/{current['id']}/speech/regenerate",
        json={"expected_version": patched.json()["version"]},
        headers={"Idempotency-Key": "question-speech-regenerate-1"},
    )
    assert regenerated.status_code == 202, regenerated.text
    assert regenerated.json()["speech_status"] == "pending"
    replayed = api.post(
        f"/api/v1/questions/{current['id']}/speech/regenerate",
        json={"expected_version": patched.json()["version"]},
        headers={"Idempotency-Key": "question-speech-regenerate-1"},
    )
    assert replayed.status_code == 202
    assert replayed.json()["job_id"] == regenerated.json()["job_id"]
    asyncio.run(OutboxWorker(get_store()).run_once())
    regenerated = api.get(f"/api/v1/questions/{current['id']}")
    archived = api.patch(
        f"/api/v1/questions/{current['id']}",
        json={"expected_version": regenerated.json()["version"], "status": "archived"},
    )
    assert archived.json()["status"] == "archived"


def test_candidate_patch() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "A", "email": "a@example.com", "phone": "13800138000"},
    ).json()
    patched = api.patch(
        f"/api/v1/candidate-profiles/{candidate['id']}",
        json={"expected_version": candidate["version"], "name": "Updated", "status": "archived"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Updated"
    assert patched.json()["status"] == "archived"


class _RealTTSGateway:
    async def invoke(self, capability, request, *, route=None):
        pcm = b"\x01\x00\x02\x00"
        wave_body = (
            b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 24_000, 48_000, 2, 16)
            + b"data"
            + struct.pack("<I", len(pcm))
            + pcm
        )
        content = b"RIFF" + struct.pack("<I", len(wave_body)) + wave_body
        return TTSSynthesizeResponse(
            audio_uri="data:audio/wav;base64,%s" % base64.b64encode(content).decode("ascii"),
            content_type="audio/wav",
            duration_ms=1000,
            content_hash="sha256:provider",
            provider=ProviderMeta(
                provider_id="real_tts_test",
                model="voice",
                request_id="vendor_1",
                latency_ms=1,
            ),
        )


def test_real_tts_asset_is_copied_to_private_storage(tmp_path) -> None:
    store = InMemoryStore()
    persistence = persistence_for(store)
    storage = LocalPrivateFileAdapter(
        tmp_path,
        signer=FileAccessSigner("speech-signing-secret-at-least-32-characters"),
    )
    catalog = CatalogService(
        store,
        persistence=persistence,
        gateway=_RealTTSGateway(),
        storage=storage,
    )
    position = catalog.create_position({"code": "tts", "name": "TTS"})
    knowledge_base = catalog.create_knowledge_base(position["id"], {"name": "TTS bank"})
    question = asyncio.run(
        catalog.create_question(
            knowledge_base["id"],
            {
                "title": "Audio",
                "question_text": "Explain audio",
                "standard_answer": "Audio answer",
                "key_points": ["audio"],
                "skills": ["audio"],
                "rubric": {"semantic": 1},
            },
        )
    )
    asyncio.run(catalog.process_speech_work(question["job_id"]))
    question = catalog.get_question(question["id"])
    with persistence.transaction("org_default") as transaction:
        asset = transaction.question_speech_assets.get(question["speech_asset_id"])
        file_object = transaction.file_objects.get(asset["file_object_id"])
    assert asset["production_ready"] is True
    assert asset["audio_uri"].startswith("private-file://")
    assert asset["content_hash"] == file_object["checksum"]
    stored = storage.open(file_object["object_key"])
    assert stored[:4] == b"RIFF"
    assert struct.unpack_from("<I", stored, 4)[0] == len(stored) - 8
    assert stored[8:12] == b"WAVE"
