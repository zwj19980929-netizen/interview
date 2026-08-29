import { candidateFeature } from "./candidate/index.js";
import { interviewsFeature } from "./interviews/index.js";
import { modelsFeature } from "./models/index.js";
import { plansFeature } from "./plans/index.js";
import { questionsFeature } from "./questions/index.js";
import { workflowFeature } from "./workflow/index.js";

export const overviewFeature = Object.freeze({
  id: "overview",
  label: "总览",
  icon: "layout-dashboard",
  roles: ["admin", "interviewer"],
});

export const workspaceFeatures = Object.freeze([
  overviewFeature,
  questionsFeature,
  workflowFeature,
  plansFeature,
  interviewsFeature,
  modelsFeature,
]);

export const publicFeatures = Object.freeze([candidateFeature]);
