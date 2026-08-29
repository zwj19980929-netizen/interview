export function createAvatarDeliveryRuntime({
  onMediaChange = () => {},
  onSpeakingChange = () => {},
  closeSession = async () => {},
  audioFactory = (source) => new Audio(source),
  speechEngine = globalThis.speechSynthesis,
  utteranceFactory = (text) => new SpeechSynthesisUtterance(text),
} = {}) {
  let active = null;
  let audio = null;

  const setSpeaking = (value) => onSpeakingChange(Boolean(value));

  const stop = async () => {
    if (audio) {
      audio.pause();
      audio.onplay = null;
      audio.onended = null;
      audio.onerror = null;
      audio = null;
    }
    speechEngine?.cancel?.();
    const previous = active;
    active = null;
    setSpeaking(false);
    onMediaChange(null);
    if (previous?.session_id) await closeSession(previous);
  };

  const play = async (delivery) => {
    await stop();
    active = delivery;
    onMediaChange(delivery);

    // TODO(cloud-avatar-expansion): this same runtime continues to own Tencent
    // WebRTC/SFU media now and can host additional cloud player adapters later.
    if (["video", "webrtc"].includes(delivery.mode) && delivery.stream_url) {
      setSpeaking(true);
      return delivery;
    }

    if (delivery.mode === "audio" && delivery.audio_uri) {
      audio = audioFactory(delivery.audio_uri);
      audio.onplay = () => setSpeaking(true);
      audio.onended = () => setSpeaking(false);
      audio.onerror = () => setSpeaking(false);
      await audio.play();
      return delivery;
    }

    if (speechEngine && delivery.text) {
      const utterance = utteranceFactory(delivery.text);
      utterance.lang = "zh-CN";
      utterance.onstart = () => setSpeaking(true);
      utterance.onend = () => setSpeaking(false);
      utterance.onerror = () => setSpeaking(false);
      speechEngine.speak(utterance);
    }
    return delivery;
  };

  return { play, stop };
}
