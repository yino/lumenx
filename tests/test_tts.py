from __future__ import annotations

from src.audio.tts import TTSProcessor


def test_cosyvoice_retries_websocket_startup_timeout_once(
    tmp_path,
    monkeypatch,
) -> None:
    attempts = []
    sleeps = []

    class FakeSynthesizer:
        def __init__(self, **parameters) -> None:
            self.parameters = parameters
            self.closed = False
            attempts.append(self)

        def call(self, text):
            assert text == "你终于来了。"
            if len(attempts) == 1:
                raise TimeoutError("websocket connection timeout")
            return b"ID3audio"

        def close(self):
            self.closed = True

        @staticmethod
        def get_last_request_id():
            return "speech-request-2"

        @staticmethod
        def get_first_package_delay():
            return 123.0

    monkeypatch.setattr(
        "dashscope.audio.tts_v2.SpeechSynthesizer",
        FakeSynthesizer,
    )
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
    output_path = tmp_path / "dialogue.mp3"

    result = TTSProcessor(api_key="test-key", model="cosyvoice-v2").synthesize(
        "你终于来了。",
        str(output_path),
        voice="longcheng_v2",
    )

    assert len(attempts) == 2
    assert attempts[0].closed is True
    assert sleeps == [1.0]
    assert output_path.read_bytes() == b"ID3audio"
    assert result == (str(output_path), 123.0, "speech-request-2")
