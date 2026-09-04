import base64

import pytest

from app.core.prompt.realtime_dialogue import (
    approved_spoken_response_instruction,
    session_instruction,
)
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.dialogue import ValidatedSpeechDialogueStream
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ProviderMeta,
    RealtimeSpeechDialogueEvent,
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


@pytest.mark.anyio
async def test_realtime_audio_is_withheld_until_final_text_matches_approved_act() -> None:
    request = RealtimeSpeechDialogueRequest(
        interview_id="iv_unapproved_audio",
        turn_id="turn_1",
        input_audio=StreamingAudioConfig(
            content_type="audio/pcm", sample_rate_hz=16000, channels=1
        ),
        output_audio=StreamingAudioConfig(
            content_type="audio/pcm", sample_rate_hz=24000, channels=1
        ),
        session_instructions=session_instruction(),
    )

    class DeviatingProvider:
        stream_id = "dialogue_deviating"
        ready_events = [
            RealtimeSpeechDialogueEvent(
                stream_id=stream_id, sequence=1, type="dialogue.ready"
            )
        ]

        async def send_audio(self, chunk):
            return []

        async def commit(self, command, on_event=None):
            events = [
                RealtimeSpeechDialogueEvent(
                    stream_id=self.stream_id,
                    sequence=2,
                    type="output.audio.delta",
                    audio_base64=base64.b64encode(b"\x00\x00" * 100).decode(),
                    provider=ProviderMeta(
                        provider_id="test",
                        model="test",
                        request_id="request_1",
                        latency_ms=1,
                    ),
                ),
                RealtimeSpeechDialogueEvent(
                    stream_id=self.stream_id,
                    sequence=3,
                    type="output.transcript.final",
                    text="这不是批准后的追问",
                    is_final=True,
                ),
                RealtimeSpeechDialogueEvent(
                    stream_id=self.stream_id,
                    sequence=4,
                    type="output.audio.done",
                ),
            ]
            for event in events:
                await on_event(event)
            return []

        async def interrupt(self):
            return []

        async def abort(self):
            return None

    stream = ValidatedSpeechDialogueStream(DeviatingProvider(), request)
    await stream.send_audio(b"\x00\x00" * 10)
    released = []
    with pytest.raises(ProviderError, match="approved follow-up text"):
        await stream.commit(
            RealtimeSpeechResponseCommand(
                spoken_text="批准后的追问",
                response_instructions=approved_spoken_response_instruction(
                    "批准后的追问"
                ),
            ),
            on_event=released.append,
        )
    assert released == []
