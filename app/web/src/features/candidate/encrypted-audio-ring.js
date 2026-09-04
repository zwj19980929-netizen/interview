const encoder = new TextEncoder();

export class EncryptedAudioRingBuffer {
  constructor({
    cryptoImpl = globalThis.crypto,
    clock = () => Date.now(),
    retentionMs = 30_000,
    maxBytes = 2 * 1024 * 1024,
  } = {}) {
    if (!cryptoImpl?.subtle || !cryptoImpl?.getRandomValues) {
      throw new Error("浏览器不支持加密音频恢复缓冲");
    }
    this.crypto = cryptoImpl;
    this.clock = clock;
    this.retentionMs = retentionMs;
    this.maxBytes = maxBytes;
    this.entries = [];
    this.totalBytes = 0;
    this.keyPromise = cryptoImpl.subtle.generateKey(
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"],
    );
  }

  async append(sequence, value) {
    const bytes = toUint8Array(value);
    if (!bytes.byteLength) return;
    const key = await this.keyPromise;
    const iv = new Uint8Array(12);
    this.crypto.getRandomValues(iv);
    const additionalData = encoder.encode(`candidate-audio:${sequence}`);
    const ciphertext = await this.crypto.subtle.encrypt(
      { name: "AES-GCM", iv, additionalData },
      key,
      bytes,
    );
    const entry = {
      sequence,
      capturedAt: this.clock(),
      iv,
      ciphertext,
      byteLength: ciphertext.byteLength,
      clearByteLength: bytes.byteLength,
    };
    this.entries.push(entry);
    this.totalBytes += entry.byteLength;
    this.#trim();
  }

  acknowledgeThrough(sequence) {
    // Acknowledged frames remain encrypted in the 30-second ring so a server
    // can explicitly request replay, but normal reconnect only replays newer
    // frames and therefore cannot duplicate accepted audio.
    for (const entry of this.entries) {
      if (entry.sequence <= sequence) entry.acknowledged = true;
    }
    this.#trim();
  }

  async replayUnacknowledged() {
    const key = await this.keyPromise;
    const output = [];
    for (const entry of this.entries) {
      if (entry.acknowledged) continue;
      const additionalData = encoder.encode(`candidate-audio:${entry.sequence}`);
      const clear = await this.crypto.subtle.decrypt(
        { name: "AES-GCM", iv: entry.iv, additionalData },
        key,
        entry.ciphertext,
      );
      output.push({ sequence: entry.sequence, audio: clear });
    }
    return output;
  }

  async replayRange(firstSequence, lastSequence) {
    const key = await this.keyPromise;
    const output = [];
    for (const entry of this.entries) {
      if (entry.sequence < firstSequence || entry.sequence > lastSequence) continue;
      const additionalData = encoder.encode(`candidate-audio:${entry.sequence}`);
      const clear = await this.crypto.subtle.decrypt(
        { name: "AES-GCM", iv: entry.iv, additionalData },
        key,
        entry.ciphertext,
      );
      output.push({
        sequence: entry.sequence,
        capturedAt: entry.capturedAt,
        byteLength: entry.clearByteLength,
        audio: clear,
      });
    }
    return output;
  }

  clear() {
    this.entries = [];
    this.totalBytes = 0;
  }

  snapshot() {
    this.#trim();
    return {
      retainedFrames: this.entries.length,
      unacknowledgedFrames: this.entries.filter((entry) => !entry.acknowledged).length,
      encryptedBytes: this.totalBytes,
      retentionMs: this.retentionMs,
    };
  }

  #trim() {
    const cutoff = this.clock() - this.retentionMs;
    while (
      this.entries.length
      && (this.entries[0].capturedAt < cutoff || this.totalBytes > this.maxBytes)
    ) {
      this.totalBytes -= this.entries.shift().byteLength;
    }
  }
}

function toUint8Array(value) {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  throw new TypeError("audio frame must be an ArrayBuffer or typed array");
}
