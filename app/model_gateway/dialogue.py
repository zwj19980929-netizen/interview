import base64
import binascii
import os
from typing import Any, Awaitable, Callable, List, Optional, Protocol

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    RealtimeSpeechDialogueEvent,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
)


class ProviderSpeechDialogueStream(Protocol):
    stream_id: str
    ready_events: List[RealtimeSpeechDialogueEvent]

    async def send_audio(self, chunk: bytes) -> List[RealtimeSpeechDialogueEvent]: ...

    async def commit(
        self,
        command: RealtimeSpeechResponseCommand,
        on_event: Optional[Callable[[RealtimeSpeechDialogueEvent], Awaitable[None]]] = None,
    ) -> List[RealtimeSpeechDialogueEvent]: ...

    async def interrupt(self) -> List[RealtimeSpeechDialogueEvent]: ...

    async def abort(self) -> None: ...


class ValidatedSpeechDialogueStream:
    """Deep stream seam: bounds audio and validates normalized S2S event order."""

    def __init__(
        self,
        provider_stream: ProviderSpeechDialogueStream,
        request: RealtimeSpeechDialogueRequest,
    ) -> None:
        self._stream = provider_stream
        self.request = request
        self.stream_id = provider_stream.stream_id
        self.max_chunk_bytes = int(
            os.getenv("INTERVIEWER_DIALOGUE_STREAM_MAX_CHUNK_BYTES", "1048576")
        )
        self.max_total_bytes = int(
            os.getenv("INTERVIEWER_DIALOGUE_STREAM_MAX_BYTES", "52428800")
        )
        self.max_output_bytes = int(
            os.getenv("INTERVIEWER_DIALOGUE_OUTPUT_MAX_BYTES", "20971520")
        )
        self.byte_count = 0
        self.output_byte_count = 0
        self._last_sequence = 0
        self._closed = False
        self.ready_events = self._validate(provider_stream.ready_events)

    async def send_audio(self, chunk: bytes) -> List[RealtimeSpeechDialogueEvent]:
        if self._closed:
            raise ProviderError(
                "provider_stream_closed", "Speech dialogue stream is closed.", retryable=False
            )
        if not chunk:
            return []
        if len(chunk) > self.max_chunk_bytes:
            raise ProviderError(
                "provider_audio_chunk_too_large",
                "Speech dialogue audio chunk exceeds the limit.",
                retryable=False,
            )
        self.byte_count += len(chunk)
        if self.byte_count > self.max_total_bytes:
            raise ProviderError(
                "provider_audio_stream_too_large",
                "Speech dialogue audio stream exceeds the limit.",
                retryable=False,
            )
        return self._validate(await self._stream.send_audio(chunk))

    async def commit(
        self,
        command: RealtimeSpeechResponseCommand,
        on_event: Optional[Callable[[RealtimeSpeechDialogueEvent], Awaitable[None]]] = None,
    ) -> List[RealtimeSpeechDialogueEvent]:
        if self._closed:
            raise ProviderError(
                "provider_stream_closed", "Speech dialogue stream is closed.", retryable=False
            )
        if self.byte_count <= 0:
            raise ProviderError(
                "provider_audio_unavailable",
                "Speech dialogue requires candidate audio before commit.",
                retryable=False,
            )
        events: List[RealtimeSpeechDialogueEvent] = []

        async def validate_and_collect(raw: RealtimeSpeechDialogueEvent) -> None:
            validated = self._validate([raw])
            events.extend(validated)

        returned = await self._stream.commit(command, on_event=validate_and_collect)
        if returned:
            validated = self._validate(returned)
            events.extend(validated)
        final_text = "".join(
            item.text for item in events if item.type == "output.transcript.final"
        ).strip()
        if any(item.type == "output.audio.delta" for item in events) and not final_text:
            raise ProviderError(
                "provider_final_transcript_missing",
                "Realtime speech provider returned audio without a verifiable final transcript.",
                retryable=False,
            )
        if final_text and self._normalize(final_text) != self._normalize(command.spoken_text):
            raise ProviderError(
                "provider_dialogue_response_mismatch",
                "Realtime speech provider did not preserve the approved follow-up text.",
                retryable=False,
            )
        if any(item.type == "dialogue.closed" for item in events):
            self._closed = True
        # Provider audio is withheld until its final transcript has been
        # checked against the exact ApprovedConversationAct.  This deliberately
        # trades speculative playback for the guarantee that unapproved speech
        # can never reach the candidate.
        if on_event:
            for event in events:
                await on_event(event)
        return events

    async def interrupt(self) -> List[RealtimeSpeechDialogueEvent]:
        if self._closed:
            return []
        return self._validate(await self._stream.interrupt())

    async def abort(self) -> None:
        if not self._closed:
            await self._stream.abort()
            self._closed = True

    def _validate(self, values: List[Any]) -> List[RealtimeSpeechDialogueEvent]:
        events: List[RealtimeSpeechDialogueEvent] = []
        for raw in values:
            event = (
                raw
                if isinstance(raw, RealtimeSpeechDialogueEvent)
                else RealtimeSpeechDialogueEvent.model_validate(raw)
            )
            if event.stream_id != self.stream_id or event.sequence <= self._last_sequence:
                raise ProviderError(
                    "provider_schema_invalid",
                    "Speech dialogue event identity or sequence is invalid.",
                    retryable=True,
                )
            if event.type == "output.audio.delta":
                try:
                    decoded = base64.b64decode(event.audio_base64, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ProviderError(
                        "provider_schema_invalid",
                        "Speech dialogue audio delta is not valid base64.",
                        retryable=True,
                    ) from exc
                if not decoded:
                    raise ProviderError(
                        "provider_schema_invalid",
                        "Speech dialogue audio delta is empty.",
                        retryable=True,
                    )
                self.output_byte_count += len(decoded)
                if self.output_byte_count > self.max_output_bytes:
                    raise ProviderError(
                        "provider_output_audio_too_large",
                        "Speech dialogue output audio exceeds the limit.",
                        retryable=False,
                    )
            if event.type == "output.transcript.final" and (not event.is_final or not event.text.strip()):
                raise ProviderError(
                    "provider_schema_invalid",
                    "Speech dialogue final transcript is incomplete.",
                    retryable=True,
                )
            self._last_sequence = event.sequence
            events.append(event)
        return events

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(character.casefold() for character in value if character.isalnum())
