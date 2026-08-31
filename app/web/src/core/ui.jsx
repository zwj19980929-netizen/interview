import { useId, useState } from "react";
import { useWorkbench } from "./WorkbenchProvider.jsx";

export const splitComma = (value) => String(value || "").split(/[,，]/).map((item) => item.trim()).filter(Boolean);
export const splitLines = (value) => String(value || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
export const formatDate = (value) => value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "-";

const statusLabels = {
  active: "启用",
  archived: "已归档",
  completed: "已完成",
  configuration_required: "待配置语音",
  draft: "草稿",
  failed: "失败",
  dead_letter: "重试耗尽",
  invalid: "无效",
  imported: "已导入",
  importing: "导入中",
  model_required: "待配置模型",
  pending: "处理中",
  queued: "排队中",
  rebuilding: "重新生成中",
  ready: "就绪",
  report_ready: "报告就绪",
  reviewing: "待审核",
  scheduled: "已预约",
  generating: "生成中",
  running: "生成中",
  stopping: "停止中",
  stopped: "已停止",
  cancelled: "已取消",
  superseded: "已被新配置替代",
  untested: "待测试",
  qualified: "符合",
  unqualified: "不符合",
  manual_review: "待人工复核",
  unavailable: "暂未接入",
  valid: "有效",
};

export function Field({ label, children, full = false, hint }) {
  const helpId = useId();
  const labelText = typeof label === "string" ? label : "字段";
  return <label className={`field${full ? " field-full" : ""}`}><span className="field-label-row"><span>{label}</span>{hint && <span className="field-help"><span className="field-help-trigger" tabIndex={0} aria-label={`${labelText}说明`} aria-describedby={helpId} title={hint}>?</span><span className="field-help-popover" id={helpId} role="tooltip">{hint}</span></span>}</span>{children}</label>;
}

export function Empty({ title, copy }) {
  return <div className="empty-state"><div><strong>{title}</strong><span>{copy}</span></div></div>;
}

export function Status({ value }) {
  const normalized = value || "draft";
  return <span className={`status-badge status-${normalized}`}>{statusLabels[normalized] || normalized}</span>;
}

export function ResourceCard({ title, meta, status, actions, children }) {
  return <article className="resource-card"><div className="resource-card-main"><div className="resource-card-title"><strong>{title}</strong>{status && <Status value={status} />}</div>{meta && <small>{meta}</small>}{children}</div>{actions && <div className="resource-card-actions">{actions}</div>}</article>;
}

export function SubmitButton({ busy, children, variant = "primary" }) {
  return <button className={`button button-${variant}`} type="submit" disabled={busy}>{busy ? "处理中…" : children}</button>;
}

export function ModalForm({ onSubmit, children, submitLabel = "保存", submitVariant = "primary", submitDisabled = false }) {
  const [busy, setBusy] = useState(false);
  const { toast } = useWorkbench();
  const handle = async (event) => {
    event.preventDefault();
    setBusy(true);
    try { await onSubmit(new FormData(event.currentTarget)); }
    catch (error) { toast("操作失败", error.message, "error"); setBusy(false); }
  };
  return <form onSubmit={handle}><div className="form-grid">{children}</div><div className="modal-footer"><button className={`button button-${submitVariant}`} type="submit" disabled={busy || submitDisabled}>{busy ? "处理中…" : submitLabel}</button></div></form>;
}
