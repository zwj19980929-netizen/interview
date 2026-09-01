import { describe, expect, it, vi } from "vitest";

import { configurationIdentity, requestWithLatestVersion } from "./concurrency.js";

describe("latest-version commands", () => {
  it("absorbs operational version changes and retries one narrow race", async () => {
    const snapshot = { id: "model_1", version: 4, configuration_revision: 2 };
    const latestVersions = [6, 7];
    const request = vi.fn(async () => ({ ...snapshot, version: latestVersions.shift() || 7 }));
    const perform = vi.fn()
      .mockRejectedValueOnce(Object.assign(new Error("conflict"), { code: "PERSISTENCE_CONFLICT" }))
      .mockResolvedValueOnce({ saved: true });

    const result = await requestWithLatestVersion({
      request,
      resourcePath: "/models/model_1",
      snapshot,
      identity: configurationIdentity(["display_name"]),
      perform,
    });

    expect(result).toEqual({ saved: true });
    expect(perform.mock.calls.map(([resource]) => resource.version)).toEqual([6, 7]);
  });

  it("fails closed when configuration semantics changed", async () => {
    const snapshot = { id: "model_1", version: 4, configuration_revision: 2 };
    const request = vi.fn(async () => ({ ...snapshot, version: 5, configuration_revision: 3 }));
    const perform = vi.fn();

    await expect(requestWithLatestVersion({
      request,
      resourcePath: "/models/model_1",
      snapshot,
      identity: configurationIdentity(["display_name"]),
      perform,
    })).rejects.toMatchObject({ code: "RESOURCE_SEMANTIC_CONFLICT" });
    expect(perform).not.toHaveBeenCalled();
  });
});
