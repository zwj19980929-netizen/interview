import { describe, expect, it } from "vitest";

import { EncryptedAudioRingBuffer } from "./encrypted-audio-ring.js";

describe("encrypted candidate audio recovery ring", () => {
  it("decrypts only the requested ordered range and tracks acknowledgements", async () => {
    let now = 1_000;
    const ring = new EncryptedAudioRingBuffer({
      clock: () => now,
      retentionMs: 30_000,
      maxBytes: 2 * 1024 * 1024,
    });
    await ring.append(1, new Uint8Array([1, 0]).buffer);
    now += 10;
    await ring.append(2, new Uint8Array([2, 0]).buffer);
    now += 10;
    await ring.append(3, new Uint8Array([3, 0]).buffer);

    const replay = await ring.replayRange(2, 3);
    expect(replay.map((item) => item.sequence)).toEqual([2, 3]);
    expect(Array.from(new Uint8Array(replay[0].audio))).toEqual([2, 0]);
    expect(Array.from(new Uint8Array(replay[1].audio))).toEqual([3, 0]);
    expect(replay.map((item) => item.capturedAt)).toEqual([1_010, 1_020]);

    ring.acknowledgeThrough(2);
    expect(ring.snapshot()).toMatchObject({
      retainedFrames: 3,
      unacknowledgedFrames: 1,
      retentionMs: 30_000,
    });
  });
});
