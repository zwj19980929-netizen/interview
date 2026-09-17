// Candidate-facing copy is deliberately separate from diagnostic messages.
// Never render provider text or technical exception messages in the interview.
export function candidateNotice(problem, pauseConfirmed = false) {
  if (!problem) return null;
  if (problem.recoverable === false) {
    if (problem.pausePending) return { title: "正在暂停面试", detail: "请先停止作答，稍等片刻。", urgent: true };
    return pauseConfirmed
      ? { title: "面试已暂停", detail: "请联系面试安排人协助恢复。", urgent: true }
      : { title: "连接中断，请先停止作答", detail: "请联系面试安排人确认后再继续。", urgent: true };
  }
  if (problem.action === "retry_planning") {
    return { title: "下一话题暂时没准备好", detail: "你的回答已保留，可以重试继续面试。", retry: true, action: "planning.retry" };
  }
  if (["UNDERSTANDING_UNAVAILABLE", "SUPPLEMENT_REPLY_UNAVAILABLE"].includes(problem.code)) {
    return { title: "正在整理你的回答", detail: "稍等片刻，也可以继续补充。" };
  }
  if (["UNDERSTANDING_RETRY_EXHAUSTED", "SUPPLEMENT_REPLY_RETRY_EXHAUSTED"].includes(problem.code)) {
    return { title: "刚才的回答暂时没处理好", detail: "请重试，或继续补充。", retry: true };
  }
  if (["TRANSCRIPT_UNAVAILABLE", "ENDPOINT_UNCERTAIN"].includes(problem.code)) {
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
