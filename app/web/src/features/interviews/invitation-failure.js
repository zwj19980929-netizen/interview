const CHECK_LABELS = {
  candidate_pools: "面试题目候选池",
  assessment_time_budget: "本场考察所需时间",
  question_speech: "题目语音资源",
  experience_question_speech: "经历题语音资源",
  answer_evaluation: "回答评分服务",
  stt_batch: "回答语音识别服务",
  stt_streaming: "实时语音识别服务",
  warmup_stt_streaming: "入场语音校准服务",
  turn_understanding: "对话理解服务",
  controlled_followup: "追问生成服务",
  agent_expression_tts: "面试官语音合成服务（TTS）",
  agent_realtime_speech: "实时语音对话服务",
  candidate_audio_storage: "录音存储服务",
  formal_audio_evidence_consent_scope: "录音设置",
  livekit_media_plane: "实时音视频服务（LiveKit）",
  livekit_authoritative_audio_ingress: "服务端收音服务",
  licensed_local_vrm: "数字人授权资源",
  interview_agent_organization_rollout: "组织面试服务开通",
  interview_agent_release_scope: "部署版本配置",
  interview_agent_acceptance_report: "部署验收检查",
};

const REASONS = {
  provider_timeout: "供应商响应超时，请稍后重试。",
  provider_billing_required: "供应商账户计费或额度受限，请管理员处理后重试。",
  provider_auth_failed: "供应商凭据验证失败，请管理员检查凭据。",
  provider_cost_limit_exceeded: "已达到应用配置的费用上限，请管理员检查费用限制。",
  provider_rate_limited: "供应商请求受限，请稍后重试。",
  provider_circuit_open: "服务连续失败，暂时停止调用，请稍后重试。",
  provider_network_error: "服务端连接供应商失败，请检查网络后重试。",
  provider_transport_unavailable: "供应商连接不可用，请检查服务连接。",
  provider_server_error: "供应商服务暂时异常，请稍后重试。",
  provider_bad_request: "供应商请求配置不匹配，请管理员检查模型和语音参数。",
  provider_capability_missing: "供应商不支持所需能力，请管理员检查模型配置。",
  provider_streaming_not_supported: "供应商不支持所需流式协议，请管理员检查语音配置。",
  provider_schema_invalid: "供应商返回格式未通过校验，请管理员检查模型配置。",
  provider_audio_final_missing: "供应商未返回完整语音，请稍后重新检查。",
  provider_audio_format_unsupported: "供应商返回的音频格式不受支持，请管理员检查语音配置。",
  provider_probe_cleanup_failed: "服务检查未能完成资源回收，请管理员检查服务。",
  provider_probe_cancelled: "服务检查已中断，请稍后重试。",
  provider_route_invalid: "模型调用配置已变化或无效，请管理员检查配置。",
  provider_route_missing: "缺少模型调用配置，请管理员完成配置。",
  provider_connection_disabled: "供应商连接已停用，请管理员检查配置。",
  provider_model_unavailable: "模型当前不可用，请管理员检查模型服务。",
  provider_not_installed: "所需供应商插件不可用，请管理员检查部署。",
  provider_not_implemented: "所需供应商能力尚未实现，请管理员检查部署。",
  ROUTE_MISSING: "缺少模型调用配置，请管理员完成配置。",
  ROUTE_DISABLED: "模型调用配置已停用，请管理员检查配置。",
  ROUTE_POLICY_INVALID: "模型调用策略无效，请管理员检查配置。",
  MODEL_CONFIGURATION_MISSING: "缺少模型配置，请管理员完成配置。",
  MODEL_CONFIGURATION_DISABLED: "模型配置已停用，请管理员检查配置。",
  MODEL_NOT_READY: "模型尚未通过验证，请管理员检查模型配置。",
  MODEL_CAPABILITY_MISMATCH: "模型不支持所需能力，请管理员检查配置。",
  PROVIDER_CONNECTION_MISSING: "缺少供应商连接，请管理员完成配置。",
  PROVIDER_CONNECTION_DISABLED: "供应商连接已停用，请管理员检查配置。",
  NON_PRODUCTION_PROVIDER: "当前模型为模拟服务，请管理员配置正式服务。",
  PROVIDER_NOT_IMPLEMENTED: "供应商能力尚未实现，请管理员检查部署。",
  PROVIDER_CAPABILITY_MISMATCH: "供应商不支持所需能力，请管理员检查配置。",
  ROUTE_CONFIGURATION_CHANGED: "配置变更后尚未完成检查，请重新检查服务。",
  ROUTE_HEALTH_EXPIRED: "服务检查结果已过期，请重新检查服务。",
  ROUTE_HEALTH_UNTESTED: "服务尚未完成检查，请重试检查。",
  ROUTE_PROBE_IN_PROGRESS: "服务检查仍在进行，请稍后重试。",
};

const COOLDOWN_REASONS = new Set([
  "provider_timeout", "provider_rate_limited", "provider_circuit_open", "provider_network_error",
  "provider_transport_unavailable", "provider_server_error", "provider_probe_failed", "provider_probe_cancelled",
]);

const known = (mapping, key) => typeof key === "string" && Object.hasOwn(mapping, key) ? mapping[key] : null;
const record = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};

function routeFailure(value, now) {
  const route = record(value);
  let message = known(REASONS, route.reason_code);
  if (!message) {
    message = known({
      checking: "服务检查仍在进行，请稍后重试。",
      expired: "服务检查结果已过期，请重新检查服务。",
      untested: "服务尚未完成检查，请重试检查。",
      configuration_invalid: "服务配置未通过检查，请管理员检查配置。",
    }, route.status) || "服务检查未通过，请管理员查看模型服务检查结果。";
  }
  if (route.status === "failed" && route.can_refresh === false && COOLDOWN_REASONS.has(route.reason_code)
    && typeof route.retry_at === "string" && Date.parse(route.retry_at) > now) {
    message += "当前处于重试冷却期，请等待冷却结束后再试。";
  }
  return message;
}

function readinessFailures(details, now) {
  if (!Array.isArray(details.checks)) return [];
  return details.checks.filter((value) => {
    const check = record(value);
    if (!Object.keys(check).length || check.blocking === false) return false;
    // Mirror AppointmentAdmission.plan_readiness's invitation gate, including
    // the production fallback and explicit invite_ready override.
    const ready = check.invite_ready ?? (details.runtime_environment === "production"
      ? check.production_ready ?? check.ready : check.ready);
    return ready !== true;
  }).slice(0, 32).map((check) => ({
    label: known(CHECK_LABELS, check.name) || "其他服务检查",
    message: check.name === "assessment_time_budget" ? "预约可用时间不足，请延长预约或重新准备更短的计划。"
      : check.route_readiness ? routeFailure(check.route_readiness, now)
      : "该项尚未通过，请管理员检查配置或资源准备情况。",
  }));
}

// Only controlled labels reach the page/toast. Provider messages and arbitrary
// details may contain credentials, internal URLs or candidate data.
export function invitationFailure(error, now = Date.now()) {
  const failure = record(error);
  const details = record(failure.details);
  if (failure.code === "APPOINTMENT_NOT_READY") {
    const checks = readinessFailures(details, now);
    return {
      title: "面试服务检查未通过",
      message: checks.length ? "以下服务或资源尚未满足签发条件。处理后可重试签发邀请。"
        : "未收到具体未通过项目，请管理员查看服务检查结果后重试。",
      checks,
    };
  }
  const knownFailure = known({
    REQUEST_TIMEOUT: ["邀请请求超时", "未能及时收到签发结果。请检查服务连接后重试。"],
    NETWORK_UNAVAILABLE: ["无法连接邀请服务", "请检查网络和 API 服务连接后重试。"],
    REQUEST_CANCELLED: ["邀请请求已取消", "预约仍保留，需要时可重新签发邀请。"],
    INVITATION_EXPIRY_INVALID: ["邀请有效期已过", "预约结束时间已过，请调整预约时间后再签发。"],
    APPOINTMENT_NOT_INVITABLE: ["预约状态不允许签发邀请", "请刷新预约列表，确认预约是否已取消或已进入后续流程。"],
    INTERVIEW_APPOINTMENT_NOT_FOUND: ["预约暂不可用", "请刷新预约列表，并确认当前账号有权访问该预约。"],
    INTERVIEW_PLAN_NOT_FOUND: ["面试计划暂不可用", "请检查预约关联的面试计划。"],
    INTERVIEW_PLAN_NOT_APPROVED: ["面试计划未获批准", "请先检查并批准预约关联的面试计划，再签发邀请。"],
    ASSESSMENT_APPOINTMENT_WINDOW_TOO_SHORT: ["预约时长不足", "本次预约时长不足以完成已确定的考察范围，请延长预约或重新准备更短的计划。"],
    ASSESSMENT_START_TIME_INSUFFICIENT: ["剩余预约时间不足", "剩余时间不足以完成本场已确定的考察范围，请重新安排预约时间。"],
    REQUEST_VALIDATION_FAILED: ["邀请请求信息无效", "请检查预约信息后重试。"],
    PERSISTENCE_CONFLICT: ["预约信息已变化", "请刷新预约列表，确认最新状态后重试。"],
  }, failure.code);
  if (knownFailure) return { title: knownFailure[0], message: knownFailure[1], checks: [] };
  const providerReason = known(REASONS, failure.code);
  if (providerReason) return { title: "邀请签发失败", message: providerReason, checks: [] };
  if (failure.status === 401 || ["AUTHENTICATION_REQUIRED", "AUTHENTICATION_INVALID"].includes(failure.code)) {
    return { title: "登录状态已失效", message: "请重新登录后继续处理已保存的预约。", checks: [] };
  }
  if (failure.status === 403 || failure.code === "AUTHORIZATION_FORBIDDEN") {
    return { title: "没有签发邀请的权限", message: "请使用有权限的账号，或联系管理员处理。", checks: [] };
  }
  return { title: "邀请签发失败", message: "暂时无法完成签发，请稍后重试；持续失败时请管理员检查服务。", checks: [] };
}
