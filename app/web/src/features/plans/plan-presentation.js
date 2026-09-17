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
  if (plan.execution_schema_version !== 3) return `${(plan.bank_slots?.length || 0) + (plan.experience_question_ids?.length || 0)} 道问题`;
  const budget = plan.assessment_contract?.budget;
  return budget?.min_root_questions && budget?.max_root_questions ? `${budget.min_root_questions}–${budget.max_root_questions} 个话题` : "根据面试情况选择话题";
}

export const planStatusLabels = { draft: "待审阅", approved: "已启用", archived: "已归档" };
