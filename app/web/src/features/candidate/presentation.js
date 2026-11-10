// Candidate-facing copy is deliberately separate from diagnostic messages.
// Never render provider text or technical exception messages in the interview.
const UNDERSTANDING_RECOVERY_CODES = new Set([
  "UNDERSTANDING_UNAVAILABLE", "SUPPLEMENT_REPLY_UNAVAILABLE",
]);
const UNDERSTANDING_RETRY_CODES = new Set([
  "UNDERSTANDING_RESULT_REJECTED", "UNDERSTANDING_RETRY_EXHAUSTED", "SUPPLEMENT_REPLY_RETRY_EXHAUSTED",
]);

export function understandingRecoveryState(problem) {
  if (problem?.recoverable !== true) return null;
  const clarifying = problem.code === "UNDERSTANDING_CLARIFICATION_REQUIRED";
  if ((clarifying || UNDERSTANDING_RETRY_CODES.has(problem.code) || UNDERSTANDING_RECOVERY_CODES.has(problem.code))
    && problem.action === "contact_support") return "blocked";
  if (clarifying) return "clarifying";
  if (UNDERSTANDING_RETRY_CODES.has(problem.code)) return "retry_required";
  return UNDERSTANDING_RECOVERY_CODES.has(problem.code) ? "retrying" : null;
}

export function warmupRetryRequired(problem) {
  return problem?.recoverable === true && (problem.action === "retry_warmup"
    || ["WARMUP_STT_PROBLEM", "WARMUP_RETRY_REQUIRED"].includes(problem.code));
}

export function candidateConnectionNotice(experience) {
  if (!experience?.session || experience.completion || experience.problem?.recoverable === false
    || ["paused", "completed"].includes(experience.phase)) return null;
  const connection = experience.connection || {};
  if (connection.control !== "connected"
    || ["reconnecting", "disconnected", "recovery"].includes(connection.media)) {
    return { title: "正在恢复本场面试", detail: "当前题目和已收到的回答会保留。请等待连接恢复，无需重复作答。" };
  }
  return null;
}

// A local safety stop is not proof that the server accepted a pause. Project
// every stopped surface from the same facts, including before open() succeeds.
export function candidateStoppedPresentation({ experience, interview, problem }) {
  const status = experience?.session?.status ?? interview?.status;
  const pauseConfirmed = status === "paused" || problem?.pauseConfirmed === true;
  if (!pauseConfirmed && problem?.recoverable !== false && experience?.phase !== "paused") return null;
  const pausePending = !pauseConfirmed && problem?.pausePending === true;
  const notice = candidateNotice({ ...problem, recoverable: false, pausePending }, pauseConfirmed);
  return {
    notice,
    header: pauseConfirmed ? "面试已暂停" : pausePending ? "正在暂停面试" : "连接已中断",
    cameraTitle: "画面已关闭",
    cameraDetail: "请按下方面试状态提示操作。",
    gateTitle: pauseConfirmed ? "面试已暂停" : pausePending ? "正在暂停面试" : "暂时无法进入面试",
    gateDetail: notice.detail,
    microphoneDetail: pauseConfirmed ? "面试已暂停，收音已停止" : pausePending ? "收音已停止，正在确认暂停结果" : "收音已停止，请查看页面提示",
  };
}

// Hidden future plan entries, warm-up captions and local microphone activity
// are not answers. HTTP may expose the newly selected first question before
// the WebSocket leaves planning; current, skipped and incomplete rows cannot
// establish a previous topic.
export function candidatePlanningPresentation({ session, history } = {}) {
  const visible = (history?.turns || []).filter(turn =>
    typeof turn.question_spoken_text === "string" && turn.question_spoken_text.trim());
  const previousTopic = visible.some(turn => typeof turn.id === "string" && turn.id
    && turn.id !== history?.current_turn_id && ["transcribing", "evaluating", "completed"].includes(turn.status));
  const submitted = Number(session?.completed_answers) > 0 || (history?.answers || []).length > 0;
  const firstQuestion = session?.completed_answers === 0 && !visible.length && !submitted
    && Array.isArray(history?.turns) && Array.isArray(history?.answers) && !history.current_turn_id;
  const subject = firstQuestion ? "第一题" : submitted || previousTopic ? "下一话题" : "面试问题";
  return {
    title: `正在准备${subject}`,
    heading: `面试官正在准备${subject}`,
    detail: firstQuestion ? "试音已完成，请稍等面试官开始提问。"
      : submitted ? "这段回答已保留，请稍等面试官继续。" : "请稍等面试官继续提问。",
    retryTitle: `${subject}暂时没准备好`,
    retryDetail: firstQuestion ? "试音已完成，可以重试开始提问。"
      : submitted ? "你的回答已保留，可以重试继续面试。" : "可以重试继续面试。",
  };
}

export function candidateNotice(problem, pauseConfirmed = false, phase = null, planningContext) {
  if (!problem) return null;
  if (problem.recoverable === false) {
    if (problem.pausePending) return { title: "正在暂停面试", detail: "请先停止作答，稍等片刻。", urgent: true };
    const audioReason = {
      AUDIO_TRACK_LOST: "面试官的语音连接已中断。",
      AUDIO_STREAM_INVALID: "面试官的语音传输不完整，暂时无法继续播放。",
      AUDIO_PLAYBACK_FAILED: "面试官的语音未能正常播放。",
      AUDIO_PLAYBACK_TIMEOUT: "面试官的语音长时间没有播放进度。",
      AUDIO_OUTPUT_UNAVAILABLE: "浏览器未能开启语音播放，请检查声音播放权限。",
    }[problem.code] || "";
    return pauseConfirmed
      ? { title: "面试已暂停", detail: `${audioReason}请联系面试安排人协助恢复。`, urgent: true }
      : { title: "连接中断，请先停止作答", detail: `${audioReason}请联系面试安排人确认后再继续。`, urgent: true };
  }
  if (problem.action === "retry_planning") {
    const presentation = candidatePlanningPresentation(planningContext);
    return { title: presentation.retryTitle, detail: presentation.retryDetail, retry: true, action: "planning.retry" };
  }
  if (problem.code === "QUESTION_AUDIO_RETRY_REQUIRED") {
    return { title: "追问语音需要重试", detail: "上一题回答已保留。点击“重试播放”收听这道追问，播完后再作答。",
      retry: true, action: "retry_question_audio", retryLabel: "重试播放" };
  }
  if (problem.code === "WARMUP_CONFIRMATION_AUDIO_UNAVAILABLE") {
    return { title: "试音确认提示音暂时不可用", detail: "字幕已保留。请核对下方字幕：正确可确认开始，不正确请选择重新试音。" };
  }
  if (warmupRetryRequired(problem)) {
    return { title: "试音流程未完成，需要重新试音", detail: "即使已经显示字幕，本次试音仍未完成。请点击下方“重新试音”，完成后再确认进入正式面试。这段试音不计分。" };
  }
  if (understandingRecoveryState(problem) === "blocked") {
    return { title: "回答处理服务暂时不可用", detail: "录音、已识别内容和确认进度已保留。请联系面试安排人恢复服务，无需重复作答。" };
  }
  if (understandingRecoveryState(problem) === "retrying") {
    const stage = problem.stage === "supplement_prompt" ? "正在恢复补充确认提示" : "正在恢复回答处理";
    return { title: stage, detail: "录音和已识别内容已保留，系统正在自动恢复此步骤。无需重说整题；想补充仍可直接开口。"
      + (phase === "awaiting_exit_confirmation" ? "本次面试尚未结束。" : "") };
  }
  if (understandingRecoveryState(problem) === "clarifying") {
    return { title: "可以继续说明", detail: "刚才这句话暂未理解清楚，可以换个说法继续；也可重试处理已收到的语音，无需重说整题。",
      retry: true, action: "retry_answer_processing", retryLabel: "重试处理" };
  }
  if (understandingRecoveryState(problem) === "retry_required") {
    return { title: "回答处理需要重试", detail: "录音和已识别内容已保留。点击“重试处理”继续处理已有回答，无需重说整题。"
      + (phase === "awaiting_exit_confirmation" ? "本次面试尚未结束。" : ""),
      retry: true, action: "retry_answer_processing", retryLabel: "重试处理" };
  }
  if (problem.code === "TRANSCRIPT_RETRY_REQUIRED") {
    return { title: "这段语音暂未识别完成", detail: "录音和已识别内容已保留。请点击“重试处理”继续处理，无需重说整题。", retry: true, retryLabel: "重试处理" };
  }
  if (problem.code === "TRANSCRIPT_UNAVAILABLE") {
    return { title: "正在确认转写", detail: "音频已收到，正在等待识别完成；暂定字幕还可能修正。" };
  }
  if (problem.code === "ENDPOINT_UNCERTAIN") {
    return { title: "还在听你说", detail: "可以继续回答，讲完后告诉我。" };
  }
  if (["CAPTURE_RECOVERING", "AGENT_EVENT_RESYNC_REQUIRED"].includes(problem.code)) {
    return { title: "连接有些慢，正在恢复", detail: "请稍等片刻。" };
  }
  if (problem.code === "CAPTURE_RETRY_REQUIRED") {
    return { title: "这段回答需要重试", detail: "请点击下方“重试本题”。" };
  }
  return { title: "请稍等片刻", detail: "暂时没有完成，请稍后再试。" };
}

export function candidateEntryMessage(error) {
  if (["NotAllowedError", "PermissionDeniedError"].includes(error?.name)) return "请允许使用麦克风和摄像头后重试。";
  if (error?.name === "NotFoundError") return "没有找到麦克风或摄像头，请连接设备后重试。";
  return candidateEntryFailure(error).detail;
}

export function candidateEntryFailure(error) {
  switch (error?.code) {
    case "INVITATION_REGISTRATION_REQUIRED":
      return { title: "请先确认本次预约", detail: "本次邀请尚未完成身份核验，请填写下方信息并确认预约。", registrationRequired: true };
    case "CONSENT_REQUIRED":
    case "AUDIO_RECORDING_CONSENT_REQUIRED":
    case "VIDEO_RECORDING_CONSENT_REQUIRED":
      return { title: "请确认本次面试授权", detail: "本次面试所需的同意信息尚未完整登记，请核对下方说明后重新确认预约。", registrationRequired: true };
    case "APPOINTMENT_TOO_EARLY":
      return { title: "尚未到入场时间", detail: "请在预约开始后进入面试。" };
    case "APPOINTMENT_WINDOW_CLOSED":
      return { title: "本次预约时间已结束", detail: "请联系面试安排人重新预约。" };
    case "ASSESSMENT_START_TIME_INSUFFICIENT":
      return { title: "剩余预约时间不足", detail: "剩余时间不足以完成本场已确定的考察范围，请联系面试安排人改期。" };
    case "ASSESSMENT_APPOINTMENT_WINDOW_TOO_SHORT":
      return { title: "预约时长不足", detail: "本次预约时长不足以完成已确定的考察范围，请联系面试安排人改期。" };
    case "APPOINTMENT_DEVICE_NOT_READY":
      return { title: "设备检查尚未通过", detail: "请重新检查设备，并确认麦克风、摄像头、浏览器和测试音均可用后再试。", recheckDevices: true };
    case "APPOINTMENT_NOT_READY":
    case "INTERVIEW_PLAN_NOT_APPROVED":
      return { title: "面试服务尚未准备好", detail: "服务端暂未完成本场面试的准备，请稍后重试；若持续出现，请联系面试安排人。" };
    case "INVITATION_EXPIRED":
      return { title: "邀请链接已过期", detail: "请联系面试安排人获取新的邀请链接。" };
    case "INVITATION_INVALID":
    case "INVITATION_UNAVAILABLE":
      return { title: "当前邀请不可用", detail: "请确认打开的是最新邀请链接，或联系面试安排人确认预约状态。" };
    case "NETWORK_UNAVAILABLE":
      return { title: "入场请求未能连接服务", detail: "刚才的连接检测结果已失效，请检查连接并点击“重新检测网络”。", invalidateNetwork: true };
    case "REQUEST_TIMEOUT":
      return { title: "入场检查超时", detail: "面试服务未能及时响应，请重新检测网络后再试；若仍超时，请联系面试安排人。", invalidateNetwork: true };
    default:
      return { title: "暂时无法完成入场检查", detail: "请稍后重试；若仍无法进入，请联系面试安排人。" };
  }
}
