import { useEffect, useRef, useState } from "react";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Status, formatDate } from "../../core/ui.jsx";

const recordingLabels = { completed: "录像已保存", hash_pending: "录像正在封存校验", stopping: "录像正在收尾", recording: "正在录制", failed: "录像处理失败", unavailable: "暂无全场录像", cancelled: "未开始录制", retention_purged: "录像已按留存策略清除" };
const fitLabels = { strong_match: "高度匹配", match: "匹配", partial_match: "部分匹配", insufficient_evidence: "证据不足", manual_review: "建议人工复核" };

export function useInterviewReview(interviewId) {
  const { API, request } = useWorkbench();
  const [review, setReview] = useState(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!interviewId) return undefined;
    let active = true;
    let timer;
    setReview(null);
    async function poll() {
      try {
        const result = await request(`${API}/interviews/${interviewId}/review`);
        if (!active) return;
        setReview(result);
        setError("");
      } catch (problem) {
        if (active) setError(problem.message || "复核数据加载失败");
      }
      if (active) timer = setTimeout(poll, 5000);
    }
    poll();
    return () => { active = false; clearTimeout(timer); };
  }, [API, request, interviewId, revision]);
  return { review, error, reload: () => setRevision((value) => value + 1) };
}

export default function InterviewReview({ interviewId, review, error, reload, canManage }) {
  const { API, request, toast } = useWorkbench();
  const [selectedId, setSelectedId] = useState("");
  const [media, setMedia] = useState(null);
  const [busy, setBusy] = useState(false);
  const [mediaError, setMediaError] = useState("");
  const generation = useRef(0);
  const pending = useRef(false);
  const player = useRef(null);
  useEffect(() => () => { generation.current += 1; }, [interviewId]);
  useEffect(() => { if (media) player.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" }); }, [media]);
  if (!review) return <section className="report-panel" aria-label="面试复核"><p role="status">{error || "正在加载评分与回放…"}</p></section>;
  const processing = review.processing || {};
  const recording = review.recording || {};
  const turns = review.turns || [];
  const selected = turns.find((turn) => turn.turn_id === selectedId) || turns[0];
  const report = review.report;
  const status = { ready: "报告已生成", pending_verification: "报告已生成", failed: "报告待处理：后台评分或报告生成失败", processing: "正在后台评分，完成后自动生成报告", awaiting_submission: "面试进行中" }[processing.report_status] || "正在同步处理状态";
  const select = (id) => { generation.current += 1; setSelectedId(id); setMedia(null); setMediaError(""); };
  const play = async (kind, turn = null) => {
    const current = ++generation.current;
    setMediaError("");
    setMedia(null);
    try {
      const grant = await request(kind === "audio"
        ? `${API}/interviews/${interviewId}/answers/${turn.answer.id}/audio-url`
        : `${API}/interviews/${interviewId}/recording-url`, { method: "POST" });
      if (generation.current !== current) return;
      setMedia({ kind, url: grant.url, label: turn ? `第 ${turn.order} 题${kind === "audio" ? "回答录音" : "视频片段"}` : "整场音视频",
        start: kind === "video" && turn ? turn.playback.start_seconds : 0,
        end: kind === "video" && turn ? turn.playback.end_seconds : null });
    } catch (problem) {
      if (generation.current === current) setMediaError(problem.message || "媒体加载失败，请重新获取播放地址");
    }
  };
  const retry = async () => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    try {
      await request(`${API}/interviews/${interviewId}/processing/retry`, { method: "POST" });
      reload();
      toast("已重新排队", "后台继续处理，完成后自动更新报告和回放");
    } catch (problem) { toast("重试失败", problem.message, "error"); }
    finally { pending.current = false; setBusy(false); }
  };
  const begin = (event) => {
    event.currentTarget.currentTime = media?.start || 0;
    event.currentTarget.play()?.catch(() => setMediaError("浏览器尚未允许自动播放，请点击播放器的播放按钮。"));
  };
  const stopAtEnd = (event) => {
    if (media?.end != null && event.currentTarget.currentTime >= media.end) event.currentTarget.pause();
  };
  return <section className="interview-review" aria-label="面试报告与回放">
    <section className="report-panel">
      <h2>面试报告与回放</h2>
      <p role="status"><strong>{status}</strong></p>
      {processing.submitted_at && <p>回答已收齐 · {formatDate(processing.submitted_at)}</p>}
      <p>已评分 {processing.completed || 0} / {processing.total || 0} 题 · 待处理 {processing.pending || 0} 题 · 失败 {processing.failed || 0} 题</p>
      {processing.failed > 0 && <p role="alert">评分失败不会丢失回答和录音。修复模型问题后可重试，评分完成后会自动生成报告。</p>}
      {error && <p role="alert">状态刷新失败：{error}</p>}
      <p>{recordingLabels[recording.status] || "暂无全场录像"} · {recording.hash_verified ? "完整性校验通过" : "尚未通过完整性校验"}</p>
      <div className="page-actions">
        <button className="button button-secondary" onClick={reload}>刷新结果</button>
        {recording.available && <button className="button button-secondary" onClick={() => play("video")}>播放整场音视频</button>}
        {canManage && (processing.can_retry || ["hash_pending", "stopping"].includes(recording.status)) && <button className="button button-primary" disabled={busy} onClick={retry}>{busy ? "正在排队…" : "重试未完成处理"}</button>}
      </div>
      {report && <div className="review-report-summary">
        {report.overall_score == null ? <p role="status"><strong>{report.score_status === "processing" ? "正在重新评分" : "正在等待有效评分"}</strong></p> : <strong className="score-value">{report.overall_score}<small> / 100</small></strong>}
        {report.recognition_notice && <p className="form-hint">{report.recognition_notice}</p>}
        <h3>{fitLabels[report.job_fit_level] || "待人工复核"}</h3>
        {report.summary && <p>{report.summary}</p>}
        <p>最终决定由企业人员完成。</p>
        {[["优势", report.strengths], ["风险与待核验项", report.risks], ["建议", report.followup_suggestions]].map(([title, items]) => items?.length > 0 && <div key={title}><h4>{title}</h4><ul>{items.map((item, index) => <li key={index}>{typeof item === "string" ? item : item.text || item.description || item.reason || JSON.stringify(item)}</li>)}</ul></div>)}
      </div>}
    </section>
    {turns.length > 0 && <section className="report-panel review-question-panel">
      <h3>逐题复核</h3>
      <div className="review-question-tabs" role="group" aria-label="选择回答题目">
        {turns.map((turn) => <button key={turn.turn_id} className={`button ${turn.turn_id === selected?.turn_id ? "button-primary" : "button-secondary"}`} aria-pressed={turn.turn_id === selected?.turn_id} onClick={() => select(turn.turn_id)}>
          第 {turn.order} 题{turn.is_followup ? " · 追问" : ""} · {turn.skip_reason === "resume_speech_not_ready" ? "语音未就绪，已跳过" : turn.answer?.evaluation_status === "failed" ? "评分失败" : turn.answer?.evaluation_status === "pending" ? "正在评分" : turn.evaluation?.score != null ? `${turn.evaluation.score}分` : turn.answer ? "待评分" : "未作答"}
        </button>)}
      </div>
      {selected && <article className="review-answer">
        <h3>{selected.question?.question_text}</h3>
        {selected.skip_reason === "resume_speech_not_ready" && <p role="status">本题读题语音在提问时尚未就绪，系统已跳过，不计入评分。</p>}
        <div className="page-actions">
          <button className="button button-secondary" disabled={!selected.playback?.audio_available} onClick={() => play("audio", selected)}>播放本题音频</button>
          <button className="button button-secondary" disabled={!selected.playback?.video_available} onClick={() => play("video", selected)}>播放本题视频</button>
        </div>
        {selected.playback?.video_available && <p className="form-hint">{selected.playback.timing_source === "server_capture_window" ? "视频按服务端回答采集时间定位。" : "历史视频按服务端题目时间窗口定位，包含读题和回答。"}音频为本题独立回答录音。</p>}
        <h4>回答转写</h4><p className="review-transcript">{selected.answer?.final_transcript || "本题没有已提交回答。"}</p>
        {selected.answer?.evaluation_status === "failed" && <p role="alert">{selected.answer.evaluation_failure_code === "provider_output_truncated" ? "评分输出达到模型上限，未生成完整结果。" : "本题后台评分失败，可重试未完成处理。"}</p>}
        {selected.answer?.evaluation_status === "pending" && selected.evaluation && <p role="status">本题正在重新评分，下方为上一版本的评分依据，完成后会自动更新。</p>}
        {selected.answer && <p className="form-hint">识别置信度：{selected.answer.stt_confidence == null ? "未知（未提供可用数值）" : `${Math.round(selected.answer.stt_confidence * 100)}%（识别服务报告值，非准确率保证）`} · {selected.answer.transcript_verified ? "转写已回听核验" : "支持按需回听纠错"}</p>}
        {selected.evaluation && <><h4>{selected.evaluation.score == null ? "本题尚无有效评分" : `评分与依据 · ${selected.evaluation.score} 分`}</h4>
          {selected.evaluation.recognition_warning && <p className="form-hint">本题部分转写存在不确定性，已按可理解的内容直接评分；回听和纠错均为可选。</p>}
          <p>{selected.evaluation.feedback}</p>
          <dl className="review-dimensions">{Object.entries(selected.evaluation.dimension_scores || {}).map(([name, value]) => <div key={name}><dt>{({ semantic_correctness: "语义正确性", key_point_coverage: "关键点覆盖", reasoning_depth: "推理深度", role_relevance: "岗位相关性", communication: "表达清晰度", specificity: "具体程度", technical_depth: "技术深度", evidence_consistency: "证据一致性", reflection: "反思" })[name] || name}</dt><dd>{value} 分</dd></div>)}</dl>
          <h4>命中证据</h4><ul>{selected.evaluation.covered_key_points?.map((point, index) => <li key={index}>{point.evidence}</li>)}</ul>
          <h4>缺失点</h4><ul>{selected.evaluation.missing_key_points?.map((point, index) => <li key={index}>{selected.question?.key_points?.find((item) => item.id === point.key_point_id)?.text || point.key_point_id}：{point.reason}</li>)}</ul>
          {!!selected.evaluation.review_flags?.length && <p>评分提示：{selected.evaluation.review_flags.map((flag) => ({ transcription_ambiguity: "转写存在歧义", low_stt_confidence: "识别置信度较低" })[flag] || flag).join("、")}</p>}
        </>}
        {canManage && selected.answer && selected.evaluation && <TranscriptVerification key={`${selected.answer.id}:${selected.evaluation.id}:${selected.answer.current_transcript_revision || 1}`} interviewId={interviewId} turn={selected} reload={reload} />}
      </article>}
    </section>}
    {(media || mediaError) && <section className="report-panel review-player-panel">
      <h3>{media?.label || "音视频回放"}</h3>
      {mediaError && <p role="alert">{mediaError} 如地址已过期，请重新点击上方播放按钮。</p>}
      {media?.kind === "video" && <video key={media.url} ref={player} src={media.url} controls playsInline preload="metadata" onLoadedMetadata={begin} onTimeUpdate={stopAtEnd} onError={() => setMediaError("视频加载失败或签名已过期。请重新获取播放地址。")}/>}
      {media?.kind === "audio" && <audio key={media.url} ref={player} src={media.url} controls autoPlay preload="metadata" onError={() => setMediaError("录音加载失败或签名已过期。请重新获取播放地址。")}/>}
    </section>}
  </section>;
}


function TranscriptVerification({ interviewId, turn, reload }) {
  const { API, request, toast } = useWorkbench();
  const [text, setText] = useState(turn.answer.final_transcript || "");
  const [reason, setReason] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const submit = async (event) => {
    event.preventDefault();
    if (submitting.current || !reviewed || !reason.trim() || !text.trim()) return;
    submitting.current = true;
    setBusy(true);
    try {
      await request(`${API}/interviews/${interviewId}/answers/${turn.answer.id}/transcription-verification`, {
        method: "POST", body: JSON.stringify({
          expected_evaluation_id: turn.evaluation.id,
          expected_transcript_revision: turn.answer.current_transcript_revision || 1,
          audio_reviewed: true, reason: reason.trim(), final_transcript: text.trim(),
        }),
      });
      toast("核验已保存", "已排队重新评分，完成后自动更新报告。");
      setReviewed(false);
      reload();
    } catch (problem) { toast("核验未保存", problem.message || "请刷新当前版本后重试", "error"); }
    finally { submitting.current = false; setBusy(false); }
  };
  return <details className="review-verification"><summary>按需回听与修正转写</summary>
    <p>请先播放本题录音。仅纠正识别错误，保持候选人实际表达，不补写知识答案；原录音和历史版本会保留。</p>
    <form onSubmit={submit}>
      <label>核验后的转写<textarea aria-label="核验后的转写" value={text} onChange={(event) => setText(event.target.value)} maxLength={12000} required rows={6}/></label>
      <label>核验说明<input aria-label="核验说明" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} required/></label>
      <label><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)}/>我已回听录音并确认上述转写与实际表达一致</label>
      <button className="button button-primary" disabled={busy || !reviewed || !reason.trim() || !text.trim() || turn.answer.evaluation_status === "pending"}>
        {busy ? "正在保存…" : "保存核验并重新评分"}
      </button>
    </form>
  </details>;
}
