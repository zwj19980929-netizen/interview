import base64

import pytest

from app.core.prompt.realtime_dialogue import (
    approved_spoken_response_instruction,
    session_instruction,
)
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import (
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
    StreamingAudioConfig,
)
from app.repositories.provider import get_store, reset_store_for_tests


@pytest.mark.anyio
async def test_mock_realtime_dialogue_streams_only_the_approved_followup() -> None:
    reset_store_for_tests()
    store = get_store()
    gateway = ModelGateway(store)
    stream = await gateway.open_speech_dialogue(
        RealtimeSpeechDialogueRequest(
            interview_id="iv_realtime_dialogue",
            turn_id="turn_root",
            input_audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
            output_audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=24000, channels=1
            ),
            session_instructions=session_instruction(),
            metadata={"development_transcript": "候选人的服务端权威转写"},
        )
    )
    assert [item.type for item in stream.ready_events] == ["dialogue.ready"]
    assert [item.type for item in await stream.send_audio(b"\x00\x00" * 1600)] == [
        "input.speech.started"
    ]

    approved = "请具体说明断线后如何恢复音频序号？"
    emitted = []

    async def collect(event) -> None:
        emitted.append(event)

    result = await stream.commit(
        RealtimeSpeechResponseCommand(
            spoken_text=approved,
            response_instructions=approved_spoken_response_instruction(approved),
        ),
        on_event=collect,
    )

    assert result == emitted
    assert next(item for item in emitted if item.type == "output.transcript.final").text == approved
    audio = next(item for item in emitted if item.type == "output.audio.delta")
    assert base64.b64decode(audio.audio_base64)
    assert emitted[-1].type == "dialogue.closed"
    assert store.model_invocations[-1]["capability"] == cap.SPEECH_DIALOGUE_REALTIME
    assert store.model_invocations[-1]["status"] == "stream_opened"


def test_realtime_prompt_is_versioned_and_rejects_empty_approved_text() -> None:
    assert "Prompt 版本" in session_instruction()
    with pytest.raises(ValueError, match="cannot be empty"):
        approved_spoken_response_instruction("  ")
