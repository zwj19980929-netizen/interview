import { useWorkbench } from "./WorkbenchProvider.jsx";

export function ModalLayer() {
  const { modal, closeModal } = useWorkbench();
  if (!modal) return null;
  return <div className="modal-layer"><div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && closeModal()}><section className="modal" role="dialog" aria-modal="true" aria-label={modal.title}><header className="modal-header"><h2>{modal.title}</h2><button className="icon-button" type="button" onClick={closeModal} aria-label="关闭">×</button></header><div className="modal-body">{modal.body}</div></section></div></div>;
}

export function ToastLayer() {
  const { toasts } = useWorkbench();
  return <div className="toast-region" aria-live="polite">{toasts.map((item) => <article className={`toast ${item.type === "error" ? "toast-error" : ""}`} key={item.id}><strong>{item.title}</strong><span>{item.message}</span></article>)}</div>;
}
