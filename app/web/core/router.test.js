import { describe, expect, it } from "vitest";
import { allowedViews, parseRoute } from "./router.js";

describe("plan workspace routes", () => {
  it("distinguishes the list, create flow and an individual plan", () => {
    expect(parseRoute("#plans")).toEqual({ view: "plans", selectedInterviewId: null });
    expect(parseRoute("#plans/new")).toEqual({ view: "plans", planAction: "create" });
    expect(parseRoute("#plans/plan_a")).toEqual({ view: "plans", selectedPlanId: "plan_a" });
    expect(parseRoute("#plans/plan_b")).toEqual({ view: "plans", selectedPlanId: "plan_b" });
  });

  it("accepts the established slash hash form and ignores unrelated query text", () => {
    expect(parseRoute("#/plans/new")).toEqual({ view: "plans", planAction: "create" });
    expect(parseRoute("#plans/plan_a?source=workspace")).toEqual({ view: "plans", selectedPlanId: "plan_a" });
  });

  it("clears detail and create identity when returning to the list", () => {
    expect(parseRoute("#plans")).not.toHaveProperty("selectedPlanId");
    expect(parseRoute("#plans")).not.toHaveProperty("planAction");
    expect(parseRoute("#plans/new")).not.toHaveProperty("selectedPlanId");
    expect(parseRoute("#plans/plan_a")).not.toHaveProperty("planAction");
  });

  it("keeps plan creation within existing author permissions", () => {
    expect(allowedViews(["interviewer"]).has(parseRoute("#plans/new").view)).toBe(true);
    expect(allowedViews(["admin"]).has(parseRoute("#plans/plan_a").view)).toBe(true);
    expect(allowedViews(["reviewer"]).has(parseRoute("#plans/new").view)).toBe(false);
    expect(allowedViews(["candidate"]).has(parseRoute("#plans/plan_a").view)).toBe(false);
  });

  it("preserves public credential and existing workspace routing", () => {
    const stored = new Map();
    const storage = { setItem: (key, value) => stored.set(key, value), getItem: (key) => stored.get(key) || null };
    expect(parseRoute("#candidate/interview_1?token=one_time", storage)).toEqual({
      view: "candidate", selectedInterviewId: "interview_1", candidateToken: "one_time", clearCandidateTokenFromHash: true,
    });
    expect(parseRoute("#candidate/interview_1", storage).candidateToken).toBe("one_time");
    expect(parseRoute("#interviews/interview_1")).toEqual({ view: "live", selectedInterviewId: "interview_1" });
    expect(parseRoute("#questions/bank_1/generation/batch_1")).toMatchObject({
      view: "questions", knowledgeBaseId: "bank_1", generation: true, generationBatchId: "batch_1",
    });
  });
});
