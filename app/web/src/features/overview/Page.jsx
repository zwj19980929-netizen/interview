import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Status } from "../../core/ui.jsx";

export default function OverviewPage() {
  const { data, navigate } = useWorkbench();
  const summary = data.questionOverview;
  const completed = data.interviews.filter((item) => item.status === "report_ready").length;
  return <><section className="page-header"><div><h1>面试运营台</h1><p>岗位题库、候选人计划、实时面试与人工复核</p></div><div className="page-actions"><button className="button button-primary" onClick={() => navigate("questions")}>管理题库</button></div></section><section className="metric-grid"><Metric label="题库题目" value={summary.total} note={`${summary.ready} 道语音就绪`} /><Metric label="面试计划" value={data.plans.length} note={`${data.roles.length} 个岗位要求`} /><Metric label="面试会话" value={data.interviews.length} note={`${completed} 份报告`} /><Metric label="模型插件" value={data.catalog.length} note={`${data.catalog.filter((item) => item.implemented).length} 个可调用`} /></section><div className="two-column section-block"><section><h2>最近题目</h2>{summary.recent.length ? summary.recent.map((item) => <article className="list-card" key={item.id}><strong>{item.title}</strong><Status value={item.validation_status} /></article>) : <Empty title="题库为空" copy="尚未创建面试题目" />}</section><section><h2>最近会话</h2>{data.interviews.slice(0, 4).length ? data.interviews.slice(0, 4).map((item) => <article className="list-card" key={item.id}><strong>{item.candidate?.name || item.id}</strong><Status value={item.status} /></article>) : <Empty title="暂无面试" copy="当前没有候选人会话" />}</section></div></>;
}

function Metric({ label, value, note }) {
  return <article className="metric-card"><div className="metric-top"><span>{label}</span></div><strong className="metric-value">{value}</strong><span className="metric-note">{note}</span></article>;
}
