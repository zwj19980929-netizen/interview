import { describe, expect, it, vi } from "vitest";

import { createAvatarDeliveryRuntime } from "./avatar-runtime.js";

describe("avatar delivery runtime", () => {
  it("plays local audio and cloud media through one lifecycle", async () => {
    const mediaChanges = [];
    const speakingChanges = [];
    const closed = [];
    const audio = {
      pause: vi.fn(),
      play: vi.fn(async function play() { this.onplay?.(); }),
    };
    const runtime = createAvatarDeliveryRuntime({
      onMediaChange: (media) => mediaChanges.push(media),
      onSpeakingChange: (speaking) => speakingChanges.push(speaking),
      closeSession: async (media) => closed.push(media.session_id),
      audioFactory: () => audio,
      speechEngine: null,
    });

    await runtime.play({ avatar_mode: "local", mode: "audio", audio_uri: "/api/v1/private-files/signed" });
    expect(audio.play).toHaveBeenCalledOnce();
    expect(speakingChanges.at(-1)).toBe(true);
    audio.onended();
    expect(speakingChanges.at(-1)).toBe(false);

    await runtime.play({ avatar_mode: "cloud", mode: "webrtc", stream_url: "wss://avatar", session_id: "session_1" });
    expect(audio.pause).toHaveBeenCalledOnce();
    expect(mediaChanges.at(-1).avatar_mode).toBe("cloud");
    await runtime.stop();
    expect(closed).toEqual(["session_1"]);
    expect(mediaChanges.at(-1)).toBeNull();
  });

  it("animates the local portrait while browser speech is active", async () => {
    const states = [];
    let utterance;
    const speechEngine = { cancel: vi.fn(), speak: vi.fn((value) => { utterance = value; value.onstart(); }) };
    const runtime = createAvatarDeliveryRuntime({
      onSpeakingChange: (speaking) => states.push(speaking),
      speechEngine,
      utteranceFactory: (text) => ({ text }),
    });

    await runtime.play({ avatar_mode: "local", mode: "browser_speech", text: "请介绍一下自己" });
    expect(utterance.lang).toBe("zh-CN");
    expect(states.at(-1)).toBe(true);
    utterance.onend();
    expect(states.at(-1)).toBe(false);
  });
});
