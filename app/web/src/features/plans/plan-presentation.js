export function planIdentity(plan, data) {
  const candidate = (data.candidates || []).find((item) => item.id === plan.candidate_profile_id);
  const role = (data.roles || []).find((item) => item.id === plan.role_requirement_id);
  const position = (data.positions || []).find((item) => item.id === (plan.job_position_id || role?.job_position_id));
  const name = candidate?.name === "[retention_purged]" ? "已清除候选人" : candidate?.name || "候选人资料不可用";
  return { name, role: position?.name || role?.title || "岗位资料不可用", requirement: role?.title || "岗位要求", candidate, position,
    duration: plan.assessment_contract?.budget?.max_duration_seconds ? plan.assessment_contract.budget.max_duration_seconds / 60 : plan.estimated_minutes || role?.interview_duration_minutes || null };
}

export function planDate(value, withTime = false) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return "日期未记录";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}) }).format(date);
}

export function planRange(plan) {
  if (plan.execution_schema_version !== 3) return `${Math.min(10, (plan.bank_slots?.length || 0) + (plan.experience_question_ids?.length || 0))} 道主问题`;
  const budget = plan.assessment_contract?.budget;
  if (!budget?.min_root_questions || !budget?.max_root_questions) return "合计最多 10 道主问题";
  const minimum = Math.min(10, budget.min_root_questions), maximum = Math.min(10, budget.max_root_questions);
  return minimum === maximum ? `${maximum} 道主问题` : `${minimum}–${maximum} 道主问题`;
}

export const planStatusLabels = { draft: "待审阅", approved: "已启用", archived: "已归档" };
