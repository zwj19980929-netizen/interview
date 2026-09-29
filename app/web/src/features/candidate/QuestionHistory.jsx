import { useEffect, useState } from "react";
import { flattenQuestions, questionTree } from "../interviews/question-tree.js";

export function useCandidateQuestionHistory({ apiBase, request, interview, token, currentTurnId, finalRevision, completedAnswers, completed, enabled }) {
  const [history, setHistory] = useState(interview);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!enabled || !interview?.id || !token) return undefined;
    let active = true;
    const controller = new AbortController();
    // Coalesce a final and its following turn change; no requests for partial captions or audio frames.
    const timer = window.setTimeout(async () => {
      try {
        const value = await request(`${apiBase}/public/interviews/${encodeURIComponent(interview.id)}`, {
          headers: { "X-Candidate-Session-Token": token }, signal: controller.signal,
        });
        if (!active) return;
        if (value?.id !== interview.id) throw new Error("Question history belongs to another interview");
        setHistory(value);
        setError(false);
      } catch {
        if (active) setError(true);
      }
    }, 80);
    return () => { active = false; window.clearTimeout(timer); controller.abort(); };
  }, [apiBase, request, interview?.id, token, currentTurnId, finalRevision, completedAnswers, completed, enabled, retry]);
  return { history, error, retry: () => setRetry(value => value + 1) };
}

export function CandidateQuestionContext({ history, currentQuestion }) {
  const tree = questionTree(history?.turns, history?.answers, currentQuestion);
  const root = tree.find(node => node.id !== currentQuestion?.turn_id
    && flattenQuestions(node.children).some(child => child.id === currentQuestion?.turn_id));
  if (!root) return null;
  return <div className="candidate-parent-question"><span>围绕主问题继续交流</span><p>{root.text}</p></div>;
}

export function CandidateQuestionHistory({ history, currentQuestion, error, retry }) {
  const tree = questionTree(history?.turns, history?.answers, currentQuestion);
  const missingAnswerText = flattenQuestions(tree).some(node => node.answer && !answerTranscript(node.answer));
  if (!tree.length && !error) return null;
  return <section className="question-history" aria-label="本场问答记录">
    <div className="question-history-heading"><h2>本场问答</h2><span>按题保留完整发言，包含作答、提问与交流</span></div>
    {(error || missingAnswerText) && <p role="status">
      {error ? "问答记录暂未同步，你可以继续面试。" : "部分已提交回答的文字记录暂未同步。"}
      {retry && <button className="button button-ghost button-small" onClick={retry}>重新同步</button>}
    </p>}
    {tree.map(node => <details key={node.id} className="question-history-topic"
      open={flattenQuestions([node]).some(item => item.id === currentQuestion?.turn_id)}>
      <summary><span>{node.label}</span><strong>{node.text}</strong>
        {node.children.length > 0 && <small>{flattenQuestions(node.children).length} 个追问</small>}</summary>
      <QuestionRecord node={node} currentTurnId={currentQuestion?.turn_id} root />
    </details>)}
  </section>;
}

function answerTranscript(answer) {
  const text = answer?.final_transcript;
  return typeof text === "string" && text.trim() ? text : "";
}

function QuestionRecord({ node, currentTurnId, root = false }) {
  const current = node.id === currentTurnId;
  return <div className={`question-record${current ? " is-current" : ""}`} data-turn-id={node.id}>
    {!root && <div className="question-record-title"><span>{node.label}</span><strong>{node.text}</strong>{current && <small>当前追问</small>}</div>}
    <div className="question-record-answer"><span>本题完整发言</span>
      <p>{node.answer
        ? answerTranscript(node.answer) || "回答已提交，文字记录暂未同步。"
        : current ? "正在交流，当前字幕显示在上方。" : "尚未形成已提交回答。"}</p></div>
    {!!node.children.length && <ol className="question-record-children">{node.children.map(child => <li key={child.id}><QuestionRecord node={child} currentTurnId={currentTurnId} /></li>)}</ol>}
  </div>;
}
