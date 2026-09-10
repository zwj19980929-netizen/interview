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
  return "暂时无法进入面试，请检查网络或重新打开邀请链接。";
}
