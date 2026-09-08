import { describe, expect, it, vi } from "vitest";
import { connectLiveKitMedia } from "./agent-experience.js";

const fake = vi.hoisted(() => ({ room: null }));
vi.mock("livekit-client", () => ({
  Room: class {
    handlers = new Map();
    localParticipant = { publishTrack: vi.fn(async (track) => ({ track })) };
    connect = vi.fn(async () => {});
    disconnect = vi.fn(async () => {});
    constructor() { fake.room = this; }
    on(event, callback) { this.handlers.set(event, callback); }
    emit(event, ...args) { this.handlers.get(event)?.(...args); }
  },
  LocalAudioTrack: class { constructor(track) { this.track = track; } },
  LocalVideoTrack: class { constructor(track) { this.track = track; } },
  Track: { Source: { Microphone: "microphone", Camera: "camera" } },
  RoomEvent: { TrackSubscribed: "subscribed", TrackUnsubscribed: "unsubscribed",
    Reconnecting: "reconnecting", Reconnected: "reconnected", Disconnected: "disconnected" },
}));

describe("LiveKit output connector", () => {
  it("hands unknown/agent tracks to approval matching without auto-attaching them", async () => {
    const onRemoteAudioTrack = vi.fn();
    const onRemoteAudioTrackRemoved = vi.fn();
    const connection = await connectLiveKitMedia({
      media: { url: "wss://synthetic.invalid", participant_token: "synthetic", video_upstream_allowed: false },
      stream: { getAudioTracks: () => [{}] }, onState: vi.fn(), onRemoteAudioTrack, onRemoteAudioTrackRemoved,
    });
    const track = { kind: "audio", sid: "TR_one", attach: vi.fn(), detach: vi.fn(() => []) };
    const publication = { trackSid: "TR_one", trackName: "approved_output" };
    const publisher = { identity: "expression:synthetic", metadata: JSON.stringify({ role: "agent" }) };
    fake.room.emit("subscribed", track, publication, publisher);
    expect(onRemoteAudioTrack).toHaveBeenCalledWith(track, publication, publisher);
    expect(track.attach).not.toHaveBeenCalled();
    fake.room.emit("unsubscribed", track);
    expect(onRemoteAudioTrackRemoved).toHaveBeenCalledWith(track);
    await connection.close();
    expect(fake.room.disconnect).toHaveBeenCalledOnce();
  });

  it("preserves the independent authorized human audio path", async () => {
    const onRemoteAudioTrack = vi.fn();
    const connection = await connectLiveKitMedia({
      media: { url: "wss://synthetic.invalid", participant_token: "synthetic", video_upstream_allowed: false },
      stream: { getAudioTracks: () => [{}] }, onState: vi.fn(), onRemoteAudioTrack,
    });
    const element = document.createElement("audio");
    const track = { kind: "audio", attach: vi.fn(() => element), detach: vi.fn(() => [element]) };
    fake.room.emit("subscribed", track, {}, { identity: "takeover:synthetic", metadata: JSON.stringify({ role: "takeover" }) });
    expect(onRemoteAudioTrack).not.toHaveBeenCalled();
    expect(track.attach).toHaveBeenCalledOnce();
    expect(element.isConnected).toBe(true);
    fake.room.emit("unsubscribed", track);
    expect(element.isConnected).toBe(false);
    await connection.close();
  });
});
