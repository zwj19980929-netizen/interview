"""A vendor sentence-begin marker is not a word that needs to be recovered."""
import pytest
from test_stt_empty_completion import EmptySocket, open_socket


@pytest.mark.anyio
@pytest.mark.parametrize("placeholder", [
    {"sentence_id": 1, "sentence_begin": True, "sentence_end": False, "begin_time": 0, "end_time": None, "text": ""},
    {"sentence_end": False, "begin_time": 0, "text": ""},
])
async def test_normal_task_finish_with_only_empty_placeholders_completes_without_words(placeholder):
    socket = EmptySocket([placeholder])
    stream = await open_socket(socket)
    await stream.send_audio(bytes(640))
    events = await stream.finish()
    assert [x.type for x in events] == ["transcript.empty", "stream.closed"]
    assert not stream.has_observed_text
    assert socket.closed
