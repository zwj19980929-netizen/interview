import { useEffect, useMemo, useRef, useState } from "react";
import { configurationIdentity, requestWithLatestVersion } from "../../../core/concurrency.js";
import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, ModalForm, Status } from "../../core/ui.jsx";

const CAPABILITY_LABELS = {
  "llm.chat_json": "结构化文本模型", "llm.chat_text": "文本生成模型", "embedding.text": "文本向量模型",
  "stt.streaming": "实时语音识别", "stt.batch": "录音转写修复", "speech.dialogue_realtime": "实时语音对话", "tts.synthesize": "语音合成", "avatar.speak": "数字人播报",
};
const ROUTE_PURPOSES = [
  ["question_speech_generation", "题目语音生成", "tts.synthesize", true, "生成岗位题和经历问题的读题语音"],
  ["resume_review", "简历审阅", "llm.chat_json", true, "提取候选人项目和岗位技能证据"],
  ["resume_experience_question_generation", "经历问题生成", "llm.chat_json", true, "根据简历证据生成待审核经历问题"],
  ["candidate_answer_transcription", "实时回答转写", "stt.streaming", true, "正式面试的服务端实时语音识别"],
  ["warmup_calibration", "试音校准", "stt.streaming", true, "正式面试前通过服务端识别确认试音结果"],
  ["interview_turn_understanding", "面试轮次理解", "llm.chat_json", true, "理解当前回答并判断是否需要继续补充"],
  ["controlled_followup", "受控追问", "llm.chat_json", true, "依据当前题目与回答生成经过审核约束的追问"],
  ["interview_agent_expression", "面试官语音表达", "tts.synthesize", true, "朗读已批准的面试官表达与追问"],
  ["candidate_answer_repair", "回答转写修复", "stt.batch", true, "流式识别失败后使用完整录音修复"],
  ["answer_evaluation", "回答评分", "llm.chat_json", true, "依据冻结题目和最终转写生成可解释评分"],
  ["interview_report", "面试报告", "llm.chat_json", true, "汇总当前评分 revision 和岗位匹配证据"],
  ["interview_question_delivery", "数字人播报", "avatar.speak", false, "使用数字人播报冻结后的当前题目"],
  ["candidate_followup_dialogue", "实时语音追问", "speech.dialogue_realtime", false, "可选的实时语音对话路径；未配置时使用语音识别、受控追问与语音合成"],
  ["question_similarity_analysis", "相似题分析", "embedding.text", false, "可选的后台题库治理能力"],
].map(([value, label, capability, required, impact]) => ({ value, label, capability, required, impact }));
const routeKey = (capability, purpose) => `${capability}::${purpose}`;
const capabilityLabel = (value) => CAPABILITY_LABELS[value] || value;
const MODEL_TYPE_LABELS = { llm: "大语言模型", embedding: "向量模型", stt: "语音识别", realtime_speech: "实时语音对话", tts: "语音合成", avatar: "数字人" };
const modelTypeLabel = (value) => MODEL_TYPE_LABELS[value] || value;
const providerIdentity = configurationIdentity(["provider_id", "display_name", "enabled", "connection_config"]);
const modelIdentity = configurationIdentity(["provider_connection_id", "model_type", "provider_model_id", "display_name", "enabled", "settings", "default_parameters"]);
const ROUTE_READINESS = {
  healthy: { label: "健康有效", style: "ready", copy: "服务端近期路由检测通过；不代表完整面试链路已验收。" },
  expired: { label: "已过期，需重测", style: "untested", copy: "上次健康证据已过期，不等于模型服务已故障。" },
  untested: { label: "未测试", style: "untested", copy: "已保存配置，尚无有效的路由检测结果。" },
  checking: { label: "检测中", style: "pending", copy: "服务端正在检测，请勿重复发起。" },
  failed: { label: "检测失败", style: "failed", copy: "最近检测未通过，请检查连接与模型配置，并按服务端重试安排再检测。" },
  configuration_invalid: { label: "配置不可用", style: "invalid", copy: "请检查路由启用状态、模型能力与厂商连接配置。" },
  unknown: { label: "状态未知，待检测", style: "untested", copy: "服务端尚未提供路由就绪状态，不能据此确认健康。" },
};
const ROUTE_REASONS = {
  ROUTE_MISSING: "路由不存在", ROUTE_DISABLED: "路由已停用", ROUTE_POLICY_INVALID: "路由策略配置无效",
  ROUTE_CONFIGURATION_CHANGED: "配置已变更，旧健康证据不再适用，请重新检测",
  MODEL_CONFIGURATION_MISSING: "模型配置不存在", MODEL_CONFIGURATION_DISABLED: "模型已停用",
  MODEL_NOT_READY: "模型尚未测试就绪", MODEL_CAPABILITY_MISMATCH: "模型能力与用途不匹配",
  PROVIDER_CONNECTION_MISSING: "厂商连接不存在", PROVIDER_CONNECTION_DISABLED: "厂商连接已停用",
  NON_PRODUCTION_PROVIDER: "开发模拟模型不能证明正式链路可用",
  PROVIDER_NOT_IMPLEMENTED: "厂商插件尚未实现", PROVIDER_CAPABILITY_MISMATCH: "厂商插件不支持所需能力",
  provider_auth_failed: "厂商认证失败，请检查授权配置", provider_bad_request: "厂商请求参数不兼容",
  provider_capability_missing: "厂商缺少所需能力", provider_circuit_open: "厂商调用正在保护冷却中",
  provider_connection_disabled: "厂商连接已停用", provider_cost_limit_exceeded: "模型调用预算已达上限",
  provider_health_failed: "厂商健康检测未通过", provider_model_unavailable: "厂商模型暂不可用",
  provider_network_error: "无法连接厂商服务", provider_not_implemented: "厂商能力尚未实现",
  provider_not_installed: "厂商插件尚未安装", provider_rate_limited: "厂商请求受到限流",
  provider_route_invalid: "路由配置无效", provider_route_missing: "缺少匹配路由",
  provider_schema_invalid: "厂商结果未通过格式校验", provider_output_truncated: "厂商结果不完整",
  provider_server_error: "厂商服务返回错误", provider_stream_closed: "厂商流已关闭",
  provider_stream_failed: "厂商流处理失败", provider_stream_interrupted: "厂商流已中断",
  provider_stream_open_failed: "厂商流未能建立", provider_streaming_not_supported: "厂商不支持所需流式协议",
  provider_timeout: "厂商响应超时", provider_transport_unavailable: "厂商传输不可用",
  provider_probe_cleanup_failed: "连接已建立，但测试连接未能正常回收，请重试",
  provider_probe_failed: "厂商检测未通过", provider_probe_cancelled: "厂商检测已取消",
};
const knownValue = (values, key) => typeof key === "string" && Object.hasOwn(values, key) ? values[key] : null;
const routeReadiness = (route) => knownValue(ROUTE_READINESS, route?.readiness?.status) || ROUTE_READINESS.unknown;
const routeTime = (value) => {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return null;
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString("zh-CN", { hour12: false }) : null;
};
const routeTestError = (error) => knownValue({
  REQUEST_TIMEOUT: "检测请求超时，请查看刷新后的状态再重试。",
  NETWORK_UNAVAILABLE: "无法连接 API 服务，请恢复连接后重试。",
  MODEL_ROUTE_NOT_FOUND: "该路由已不存在，请查看刷新后的列表。",
}, error?.code) || "路由检测未通过，请检查最新状态、连接与模型配置后重试。";

export default function ModelsPage() {
  const wb = useWorkbench();
  const { data, route, navigate, openModal, request, API, refresh, toast } = wb;
  const pendingRouteTests = useRef(new Set());
  const [routeTests, setRouteTests] = useState({});
  const reload = async (...names) => { for (const name of names) await refresh(name); };
  const readyModels = data.modelConfigurations.filter((item) => item.enabled && item.status === "ready");
  const configuredKeys = new Set(data.routes.filter((item) => item.enabled !== false).map((item) => routeKey(item.capability, item.purpose)));
  const requiredPurposes = ROUTE_PURPOSES.filter((item) => item.required);
  const requiredConfigured = requiredPurposes.filter((item) => configuredKeys.has(routeKey(item.capability, item.value))).length;
  const healthyKeys = new Set(data.routes.filter((item) => item.enabled !== false && item.readiness?.status === "healthy").map((item) => routeKey(item.capability, item.purpose)));
  const requiredHealthy = requiredPurposes.filter((item) => healthyKeys.has(routeKey(item.capability, item.value))).length;
  const providerModal = (current = null) => openModal({ title: current ? "编辑厂商连接" : "添加厂商连接", body: <ProviderForm current={current} onDone={async () => { await reload("providerConnections"); wb.closeModal(); }} /> });
  const modelModal = (current = null, providerConnectionId = null) => openModal({ title: current ? "编辑模型" : "添加模型", body: <ModelForm current={current} providerConnectionId={providerConnectionId} onDone={async () => { await reload("modelConfigurations"); wb.closeModal(); }} /> });
  const routeModal = (purpose = null) => openModal({ title: "配置业务用途", body: <RouteForm initialPurpose={purpose} onDone={async () => { await reload("routes"); wb.closeModal(); }} /> });
  const test = async (kind, id) => { try { const result = await request(`${API}/admin/${kind}/${id}/test`, { method: "POST", body: {} }); if (kind === "model-configurations") await refresh("modelConfigurations"); toast("测试成功", result.provider?.model || result.model || "调用成功"); } catch (error) { toast("测试失败", error.message, "error"); } };
  const testRoute = async (id) => {
    const current = data.routes.find((item) => item.id === id);
    if (!current || current.enabled === false || current.readiness?.status === "checking" || pendingRouteTests.current.has(id)) return;
    // The synchronous guard also covers two clicks before React renders disabled.
    pendingRouteTests.current.add(id);
    setRouteTests((items) => ({ ...items, [id]: { status: "pending", message: "正在检测路由…" } }));
    let outcome;
    try {
      await request(`${API}/admin/model-routes/${encodeURIComponent(id)}/test`, { method: "POST", body: {}, timeoutMs: 35000 });
      outcome = { status: "success", message: "检测成功，结果以服务端最新状态为准。" };
    } catch (error) {
      outcome = { status: "error", message: routeTestError(error) };
    }
    try {
      await refresh("routes");
    } catch {
      outcome = { status: "error", message: `${outcome.message} 列表刷新失败，当前显示可能已过时，请刷新页面。` };
    } finally {
      pendingRouteTests.current.delete(id);
      setRouteTests((items) => ({ ...items, [id]: outcome }));
      toast(outcome.status === "success" ? "路由检测成功" : "路由检测需要注意", outcome.message, outcome.status === "success" ? "success" : "error");
    }
  };
  const viewModel = async (item) => { try { const detail = await request(`${API}/admin/model-configurations/${item.id}`); const connection = data.providerConnections.find((provider) => provider.id === detail.provider_connection_id); openModal({ title: "模型配置详情", body: <ResourceDetails rows={[["显示名称", detail.display_name], ["厂商连接", connection?.display_name || detail.provider_connection_id], ["模型类型", detail.model_type], ["模型 ID", detail.provider_model_id], ["健康状态", <Status value={detail.status} />], ["启用状态", detail.enabled ? "启用" : "停用"], ["支持能力", detail.supported_capabilities?.map(capabilityLabel)], ["厂商设置", detail.settings], ["默认参数", detail.default_parameters]]} /> }); } catch (error) { toast("读取失败", error.message, "error"); } };
  const deleteProvider = (item) => { const count = data.modelConfigurations.filter((model) => model.provider_connection_id === item.id).length; const path = `${API}/admin/model-provider-connections/${item.id}`; openModal({ title: "删除厂商连接", body: <DeleteConfirm message={`确定删除“${item.display_name}”吗？其下 ${count} 个模型及引用这些模型的能力路由会一并删除，此操作不可恢复。`} onSubmit={async () => { const result = await requestWithLatestVersion({ request, resourcePath: path, snapshot: item, identity: providerIdentity, changedMessage: "厂商连接配置已发生变化，请重新确认删除范围", perform: (latest) => request(`${path}?expected_version=${latest.version}`, { method: "DELETE" }) }); await reload("providerConnections", "modelConfigurations", "routes"); wb.closeModal(); navigate("models"); toast("厂商已删除", `已同时删除 ${result.deleted_model_configuration_ids.length} 个模型`); }} /> }); };
  const deleteModel = (item) => { const path = `${API}/admin/model-configurations/${item.id}`; openModal({ title: "删除模型配置", body: <DeleteConfirm message={`确定删除“${item.display_name}”吗？引用它的能力路由会一并删除，同厂商下其他模型不受影响。`} onSubmit={async () => { await requestWithLatestVersion({ request, resourcePath: path, snapshot: item, identity: modelIdentity, changedMessage: "模型配置已发生变化，请重新确认后删除", perform: (latest) => request(`${path}?expected_version=${latest.version}`, { method: "DELETE" }) }); await reload("modelConfigurations", "routes"); wb.closeModal(); toast("模型已删除", "同厂商下其他模型已保留"); }} /> }); };
  const validateProvider = async (item) => { try { await request(`${API}/admin/model-provider-connections/${item.id}/validate`, { method: "POST" }); await refresh("providerConnections"); toast("连接有效", item.display_name); } catch (error) { toast("连接失败", error.message, "error"); } };
  const provider = data.providerConnections.find((item) => item.id === route.providerConnectionId);
  if (route.providerConnectionId) return <ProviderDetail provider={provider} models={data.modelConfigurations.filter((item) => item.provider_connection_id === route.providerConnectionId)} navigate={navigate} onEdit={providerModal} onValidate={validateProvider} onDelete={deleteProvider} onAddModel={(id) => modelModal(null, id)} onViewModel={viewModel} onEditModel={(item) => modelModal(item, item.provider_connection_id)} onTestModel={(id) => test("model-configurations", id)} onDeleteModel={deleteModel} />;
  return <ProviderDirectory data={data} readyModels={readyModels} requiredConfigured={requiredConfigured} requiredHealthy={requiredHealthy} requiredCount={requiredPurposes.length} routeTests={routeTests} navigate={navigate} onAddProvider={() => providerModal()} onConfigureRoute={routeModal} onTestRoute={testRoute} />;
}

function ProviderDirectory({ data, readyModels, requiredConfigured, requiredHealthy, requiredCount, routeTests, navigate, onAddProvider, onConfigureRoute, onTestRoute }) {
  return <>
    <section className="page-header"><div><h1>模型服务</h1><p>{data.providerConnections.length} 个已接入厂商 · 进入厂商后管理和测试模型</p></div><div className="page-actions"><button className="button button-secondary" onClick={() => onConfigureRoute()} disabled={!readyModels.length}>配置业务用途</button><button className="button button-primary" onClick={onAddProvider}>接入厂商</button></div></section>
    <section className="model-directory-summary" aria-label="模型服务摘要"><span><strong>{data.providerConnections.length}</strong><small>已接入厂商</small></span><span><strong>{data.modelConfigurations.length}</strong><small>模型总数</small></span><span><strong>{readyModels.length}</strong><small>模型测试就绪</small></span><span><strong>{requiredConfigured}/{requiredCount}</strong><small>核心用途已配置</small><small>{requiredHealthy} 项健康有效</small></span></section>
    {data.providerConnections.length ? <div className="model-provider-grid">{data.providerConnections.map((item) => { const models = data.modelConfigurations.filter((model) => model.provider_connection_id === item.id); const types = [...new Set(models.map((model) => model.model_type).filter(Boolean))]; const ready = models.filter((model) => model.enabled && model.status === "ready").length; return <button className="model-provider-card" type="button" key={item.id} onClick={() => navigate("models", item.id)}><span className="model-provider-card-head"><span><strong>{item.display_name}</strong><small>{item.provider_id}</small></span><Status value={item.credential_status} /></span><span className="model-provider-card-copy">{item.enabled ? "连接已启用，可在厂商页面维护模型" : "连接已停用，现有模型不会参与业务路由"}</span><span className="model-provider-card-metrics"><span><strong>{models.length}</strong><small>模型</small></span><span><strong>{ready}</strong><small>测试就绪</small></span><span><strong>{types.length}</strong><small>模型类型</small></span></span><span className="model-provider-card-types">{types.length ? types.map((type) => <span className="tag" key={type}>{modelTypeLabel(type)}</span>) : <small>尚未添加模型</small>}</span><span className="model-provider-card-enter">进入厂商管理 <b>→</b></span></button>; })}</div> : <Empty title="尚未接入模型厂商" copy="接入厂商后，进入厂商页面添加、查看和测试模型" />}
    <details className="provider-catalog section-block model-secondary-panel"><summary><span><strong>业务用途与模型插件</strong><small>{requiredConfigured}/{requiredCount} 项核心用途已配置 · {requiredHealthy} 项健康有效 · {data.catalog.filter((item) => item.implemented).length} 个插件可接入</small></span><span>展开管理</span></summary><div className="model-secondary-content"><div className="section-title-row"><div><h2>业务用途</h2><p>已配置不等于健康可用；路由就绪状态以服务端最新检测为准</p></div><button className="button button-secondary" onClick={() => onConfigureRoute()} disabled={!readyModels.length}>配置用途</button></div><RouteCoverage routes={data.routes} models={data.modelConfigurations} tests={routeTests} onConfigure={onConfigureRoute} onTest={onTestRoute} /><div className="provider-grid">{data.catalog.map((item) => <article className={`provider-card${item.implemented ? "" : " is-unavailable"}`} key={item.provider_id}><div className="provider-card-head"><div><h3>{item.display_name}</h3><small>{item.provider_id}</small></div><Status value={item.implemented ? "ready" : "unavailable"} /></div><p>{item.description || (item.implemented ? "可通过统一模型网关接入业务能力" : "插件清单已预留，当前没有可执行 adapter")}</p></article>)}</div></div></details>
  </>;
}

function ProviderDetail({ provider, models, navigate, onEdit, onValidate, onDelete, onAddModel, onViewModel, onEditModel, onTestModel, onDeleteModel }) {
  if (!provider) return <><button className="back-link" type="button" onClick={() => navigate("models")}>← 返回模型服务</button><Empty title="厂商连接不存在" copy="它可能已被删除，请返回厂商列表重新选择" /></>;
  const ready = models.filter((item) => item.enabled && item.status === "ready").length; const types = [...new Set(models.map((item) => item.model_type))];
  return <>
    <button className="back-link" type="button" onClick={() => navigate("models")}>← 返回模型服务</button>
    <section className="page-header model-provider-detail-header"><div><span className="eyebrow">{provider.provider_id}</span><div className="model-provider-title"><h1>{provider.display_name}</h1><Status value={provider.credential_status} /></div><p>在当前厂商连接下维护模型配置；每个模型保存后需要独立测试</p></div><div className="page-actions"><button className="button button-secondary" onClick={() => onEdit(provider)}>编辑连接</button><button className="button button-secondary" onClick={() => onValidate(provider)}>校验连接</button><button className="button button-danger" onClick={() => onDelete(provider)}>删除连接</button><button className="button button-primary" onClick={() => onAddModel(provider.id)} disabled={!provider.enabled}>添加模型</button></div></section>
    <section className="model-directory-summary model-provider-detail-summary" aria-label="厂商模型摘要"><span><strong>{models.length}</strong><small>模型总数</small></span><span><strong>{ready}</strong><small>测试就绪</small></span><span><strong>{types.length}</strong><small>模型类型</small></span><span><strong>{provider.enabled ? "启用" : "停用"}</strong><small>连接状态</small></span></section>
    <section className="section-block"><div className="section-title-row"><div><h2>模型</h2><p>查看模型类型、厂商模型 ID、能力与测试状态</p></div><button className="button button-primary" onClick={() => onAddModel(provider.id)} disabled={!provider.enabled}>添加模型</button></div>{models.length ? <div className="provider-model-list">{models.map((item) => <article className="provider-model-card" key={item.id}><div className="provider-model-main"><div className="provider-model-heading"><strong>{item.display_name}</strong><Status value={item.status} /></div><div className="provider-model-identity"><span className="model-type-badge"><small>模型类型</small><b>{modelTypeLabel(item.model_type)}</b><code>{item.model_type}</code></span><span><small>模型 ID</small><b>{item.provider_model_id}</b></span></div><div className="resource-card-tags">{item.supported_capabilities?.map((capability) => <span className="tag" key={capability}>{capabilityLabel(capability)}</span>)}</div></div><div className="provider-model-actions"><button className="button button-secondary button-small" onClick={() => onViewModel(item)}>查看</button><button className="button button-secondary button-small" onClick={() => onEditModel(item)}>编辑</button><button className="button button-secondary button-small" onClick={() => onTestModel(item.id)}>测试</button><button className="button button-danger button-small" onClick={() => onDeleteModel(item)}>删除</button></div></article>)}</div> : <Empty title="该厂商还没有模型" copy="点击“添加模型”，选择模型类型和具体模型；保存后执行测试" />}</section>
  </>;
}
function RouteCoverage({ routes, models, tests, onConfigure, onTest }) {
  const byKey = new Map(routes.map((route) => [routeKey(route.capability, route.purpose), route]));
  const modelById = new Map(models.map((model) => [model.id, model]));
  return <div className="route-coverage-list">{ROUTE_PURPOSES.map((definition) => {
    const route = byKey.get(routeKey(definition.capability, definition.value));
    const model = route ? modelById.get(route.primary?.model_configuration_id) : null;
    const compatible = models.some((item) => item.enabled && item.status === "ready" && item.supported_capabilities?.includes(definition.capability));
    const feedback = route && tests[route.id];
    const pending = feedback?.status === "pending" || route?.readiness?.status === "checking";
    const state = pending ? ROUTE_READINESS.checking : routeReadiness(route);
    const reason = knownValue(ROUTE_REASONS, route?.readiness?.reason_code);
    const healthy = route?.enabled !== false && !pending && route?.readiness?.status === "healthy";
    return <article className={`route-coverage-item${healthy ? " is-configured" : ""}`} key={definition.value} data-testid={`route-purpose-${definition.value}`} aria-label={definition.label}>
      <div className="route-coverage-state" aria-hidden="true"><span>{healthy ? "✓" : pending ? "…" : route || definition.required ? "!" : "○"}</span></div>
      <div className="route-coverage-copy">
        <div><strong>{definition.label}</strong><span className="tag">{capabilityLabel(definition.capability)}</span>{!definition.required && <span className="tag tag-muted">可选</span>}</div>
        <p>{definition.impact}</p>
        <small>{route ? `当前模型：${model?.display_name || route.primary?.model_configuration_id || "已配置"}` : definition.required ? "尚未配置，正式流程可能被 readiness gate 阻止" : "按需配置，不影响核心面试闭环"}</small>
        {route && <>
          <div><span className={`status-badge status-${state.style}`} data-testid={`route-readiness-${route.id}`}>{state.label}</span>{route.enabled === false && <span className="tag tag-muted">路由已停用</span>}</div>
          <small>{route.readiness?.reason_code === "ROUTE_CONFIGURATION_CHANGED" && !pending ? reason : state.copy}</small>
          {reason && route.readiness?.reason_code !== "ROUTE_CONFIGURATION_CHANGED" && !pending && <small>原因：{reason} <code>{route.readiness.reason_code}</code></small>}
          {[["checked_at", "检测时间"], ["expires_at", "有效期至"], ["retry_at", "服务端重试时间"]].map(([field, label]) => {
            const time = routeTime(route.readiness?.[field]);
            return time && <small key={field}>{label}：<time dateTime={route.readiness[field]}>{time}</time></small>;
          })}
        </>}
        {feedback && <small role={feedback.status === "error" ? "alert" : "status"}>{feedback.message}</small>}
      </div>
      <div className="route-coverage-actions">{route
        ? <button className="button button-secondary button-small" data-testid={`route-test-${route.id}`} aria-label={`${definition.label}：${pending ? "正在检测" : "测试路由"}`} disabled={pending || route.enabled === false} onClick={() => onTest(route.id)}>{pending ? "检测中…" : "测试路由"}</button>
        : <button className="button button-secondary button-small" aria-label={`配置${definition.label}`} onClick={() => onConfigure(definition.value)} disabled={!compatible}>配置</button>}
      </div>
    </article>;
  })}</div>;
}
function DeleteConfirm({ message, onSubmit }) { return <ModalForm onSubmit={onSubmit} submitLabel="确认删除" submitVariant="danger"><div className="delete-warning field-full"><strong>此操作不可撤销</strong><p>{message}</p></div></ModalForm>; }
function ResourceDetails({ rows }) { const renderValue = (value) => { if (Array.isArray(value)) return value.length ? value.join("、") : "-"; if (value && typeof value === "object" && !value.$$typeof) return <pre>{JSON.stringify(value, null, 2)}</pre>; return value ?? "-"; }; return <dl className="resource-details">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{renderValue(value)}</dd></div>)}</dl>; }

function ProviderForm({ current, onDone }) {
  const { data, request, API, toast } = useWorkbench();
  const providers = data.catalog.filter((item) => item.implemented); const preferred = providers.find((item) => item.provider_id !== "mock") || providers[0];
  const [providerId, setProviderId] = useState(current?.provider_id || preferred?.provider_id || ""); const provider = data.catalog.find((item) => item.provider_id === providerId);
  const [displayName, setDisplayName] = useState(current?.display_name || provider?.display_name || "");
  const changeProvider = (value) => { setProviderId(value); setDisplayName(data.catalog.find((item) => item.provider_id === value)?.display_name || ""); };
  return <ModalForm submitLabel={current ? "保存并重新校验" : "保存并校验连接"} onSubmit={async (form) => { const credentials = readSchema(form, provider.credential_form, "credential", Boolean(current)); const base = { display_name: displayName, enabled: form.get("enabled") !== "false", connection_config: readSchema(form, provider.connection_form, "connection") }; let item; if (current) { const path = `${API}/admin/model-provider-connections/${current.id}`; item = await requestWithLatestVersion({ request, resourcePath: path, snapshot: current, identity: providerIdentity, changedMessage: "厂商连接配置已被修改，已保留最新配置，请重新确认", perform: (latest) => request(path, { method: "PATCH", body: { ...base, expected_version: latest.version, ...(Object.keys(credentials).length ? { credentials } : {}) } }) }); } else item = await request(`${API}/admin/model-provider-connections`, { method: "POST", body: { ...base, provider_id: providerId, credentials } }); try { await request(`${API}/admin/model-provider-connections/${item.id}/validate`, { method: "POST" }); } catch (error) { toast("连接已保存但校验失败", error.message, "error"); } await onDone(); }}><p className="form-intro field-full">连接只保存厂商账号和端点；具体模型将在下一步单独配置和测试。</p><Field label="模型厂商"><select className="form-select" value={providerId} onChange={(event) => changeProvider(event.target.value)} disabled={Boolean(current)}>{providers.map((item) => <option key={item.provider_id} value={item.provider_id}>{item.display_name}{item.provider_id === "mock" ? "（仅开发）" : ""}</option>)}</select></Field><Field label="连接名称"><input className="form-input" name="display_name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} required /></Field><Field label="状态"><select className="form-select" name="enabled" defaultValue={current?.enabled === false ? "false" : "true"}><option value="true">启用</option><option value="false">停用</option></select></Field><SchemaFields schema={provider?.connection_form} prefix="connection" values={current?.connection_config} /><SchemaFields schema={provider?.credential_form} prefix="credential" secretConfigured={Boolean(current)} /></ModalForm>;
}

function ModelForm({ current, providerConnectionId, onDone }) {
  const { data, request, API } = useWorkbench(); const connections = data.providerConnections.filter((item) => item.enabled);
  const [connectionId] = useState(current?.provider_connection_id || providerConnectionId || connections[0]?.id || ""); const connection = data.providerConnections.find((item) => item.id === connectionId); const [catalog, setCatalog] = useState(null); const [type, setType] = useState(current?.model_type || ""); const [modelId, setModelId] = useState(current?.provider_model_id || ""); const [displayName, setDisplayName] = useState(current?.display_name || "");
  useEffect(() => { if (!connectionId) return; let active = true; setCatalog(null); request(`${API}/admin/model-provider-connections/${connectionId}/model-catalog`).then((value) => { if (!active) return; const nextType = current?.model_type || Object.keys(value.model_types || {})[0]; const nextId = current?.provider_model_id || value.models?.find((item) => item.model_type === nextType && item.default)?.model_id || ""; const selected = value.models?.find((item) => item.model_id === nextId); setCatalog(value); setType(nextType); setModelId(nextId); setDisplayName(current?.display_name || selected?.label || nextId); }); return () => { active = false; }; }, [connectionId, API, current, request]);
  if (!catalog) return <p>正在加载模型目录…</p>;
  const models = catalog.models?.filter((item) => item.model_type === type) || []; const selected = models.find((item) => item.model_id === modelId); const typeDefinition = catalog.model_types?.[type]; const configSchema = selected?.configuration_form || typeDefinition?.configuration_form;
  const modelSuggestionListId = `model-suggestions-${connectionId}-${type}`;
  const changeType = (value) => { const next = catalog.models?.find((item) => item.model_type === value && item.default); setType(value); setModelId(next?.model_id || ""); setDisplayName(next?.label || next?.model_id || ""); };
  const changeModel = (value) => { const next = models.find((item) => item.model_id === value); setModelId(value); setDisplayName(next?.label || value); };
  return <ModalForm submitLabel={current ? "保存模型配置" : "保存模型，随后测试"} onSubmit={async (form) => { const body = { display_name: displayName, enabled: form.get("enabled") !== "false", settings: readSchema(form, configSchema, "settings"), default_parameters: readSchema(form, catalog.parameter_forms?.[type], "parameters") }; if (current) { const path = `${API}/admin/model-configurations/${current.id}`; await requestWithLatestVersion({ request, resourcePath: path, snapshot: current, identity: modelIdentity, changedMessage: "模型配置已被其他操作修改，已保留最新配置，请重新确认", perform: (latest) => request(path, { method: "PATCH", body: { ...body, expected_version: latest.version } }) }); } else await request(`${API}/admin/model-configurations`, { method: "POST", body: { ...body, provider_connection_id: connectionId, model_type: type, provider_model_id: modelId } }); await onDone(); }}><p className="form-intro field-full">模型将添加到“{connection?.display_name || connectionId}”；保存后仍是待测试状态，测试通过后才能承担业务用途。</p><Field label="所属厂商"><div className="form-readonly"><strong>{connection?.display_name || connectionId}</strong><small>{connection?.provider_id}</small></div></Field><Field label="模型类型"><select className="form-select" value={type} onChange={(event) => changeType(event.target.value)} disabled={Boolean(current)}>{Object.entries(catalog.model_types || {}).map(([key, value]) => <option key={key} value={key}>{value.label || key}</option>)}</select></Field><Field label="具体模型" hint={typeDefinition?.selection_mode === "customizable" && models.length ? "可从官方目录选择，也可输入已授权的其他模型 ID。" : null}>{typeDefinition?.selection_mode === "predefined" ? <select className="form-select" value={modelId} onChange={(event) => changeModel(event.target.value)} required>{models.map((item) => <option value={item.model_id} key={item.model_id}>{item.label || item.model_id}</option>)}</select> : <><input className="form-input" list={models.length ? modelSuggestionListId : undefined} value={modelId} onChange={(event) => changeModel(event.target.value)} required />{models.length ? <datalist id={modelSuggestionListId}>{models.map((item) => <option value={item.model_id} label={item.label || item.model_id} key={item.model_id} />)}</datalist> : null}</>}</Field><Field label="显示名称"><input className="form-input" name="display_name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} required /></Field><Field label="状态"><select className="form-select" name="enabled" defaultValue={current?.enabled === false ? "false" : "true"}><option value="true">启用</option><option value="false">停用</option></select></Field><SchemaFields schema={configSchema} prefix="settings" values={current?.settings} /><SchemaFields schema={catalog.parameter_forms?.[type]} prefix="parameters" values={current?.default_parameters} /></ModalForm>;
}

function RouteForm({ initialPurpose, onDone }) {
  const { data, request, API } = useWorkbench(); const configured = useMemo(() => new Set(data.routes.map((item) => routeKey(item.capability, item.purpose))), [data.routes]); const available = ROUTE_PURPOSES.filter((item) => !configured.has(routeKey(item.capability, item.value))); const initial = available.find((item) => item.value === initialPurpose) || available[0]; const [purpose, setPurpose] = useState(initial?.value || ""); const definition = ROUTE_PURPOSES.find((item) => item.value === purpose); const targets = data.modelConfigurations.filter((item) => item.enabled && item.status === "ready" && item.supported_capabilities?.includes(definition?.capability));
  return <ModalForm submitLabel="保存业务用途" submitDisabled={!definition || !targets.length} onSubmit={async (form) => { await request(`${API}/admin/model-routes`, { method: "POST", body: { capability: definition.capability, purpose, primary: { model_configuration_id: form.get("model_configuration_id"), timeout_s: Number(form.get("timeout_s")) }, fallbacks: [], policy: { retry_count: Number(form.get("retry_count")) }, enabled: true } }); await onDone(); }}>{available.length ? <><p className="form-intro field-full">先选择业务用途。系统会确定所需能力，并只列出兼容且已测试就绪的模型。</p><Field label="业务用途" full><select className="form-select" name="purpose" value={purpose} onChange={(event) => setPurpose(event.target.value)}>{available.map((item) => <option value={item.value} key={item.value}>{item.label}{item.required ? " · 核心" : " · 可选"}</option>)}</select></Field><div className="route-capability-summary field-full"><span>所需能力</span><strong>{capabilityLabel(definition?.capability)}</strong><small>{definition?.impact}</small></div><Field label="执行模型" full>{targets.length ? <select className="form-select" name="model_configuration_id">{targets.map((model) => <option value={model.id} key={model.id}>{model.display_name} · {model.provider_model_id}</option>)}</select> : <div className="inline-warning">没有已就绪的“{capabilityLabel(definition?.capability)}”模型。请先返回步骤 2 添加并测试对应模型。</div>}</Field><Field label="调用超时（秒）"><input className="form-input" name="timeout_s" type="number" min="1" max="300" defaultValue="30" required /></Field><Field label="失败重试次数"><input className="form-input" name="retry_count" type="number" min="0" max="3" defaultValue="1" required /></Field></> : <Empty title="所有已知业务用途都已配置" copy="可以在业务用途列表中逐项测试现有路由" />}</ModalForm>;
}

function SchemaFields({ schema, prefix, values = {}, secretConfigured = false }) { return (schema?.fields || []).map((item) => <Field label={item.label} key={`${prefix}-${item.name}`} hint={item.help}><SchemaControl item={item} name={`${prefix}__${item.name}`} value={values?.[item.name] ?? item.default} secretConfigured={secretConfigured} /></Field>); }
function SchemaControl({ item, name, value, secretConfigured }) { if (item.control === "select") return <select className="form-select" name={name} defaultValue={value}>{item.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select>; if (["textarea", "key_value"].includes(item.control)) return <textarea className="form-input" name={name} defaultValue={item.control === "key_value" && typeof value === "object" ? JSON.stringify(value) : value} />; if (item.control === "switch") return <select className="form-select" name={name} defaultValue={String(Boolean(value))}><option value="false">否</option><option value="true">是</option></select>; return <input className="form-input" name={name} type={item.control === "secret" ? "password" : item.control === "number" ? "number" : "text"} defaultValue={item.control === "secret" ? "" : value} required={item.required && !(item.control === "secret" && secretConfigured)} placeholder={item.control === "secret" && secretConfigured ? "留空保留现有密钥" : item.placeholder} />; }
function readSchema(form, schema, prefix, omitBlankSecrets = false) { const result = {}; for (const item of schema?.fields || []) { const raw = form.get(`${prefix}__${item.name}`); if (item.control === "secret" && omitBlankSecrets && !raw) continue; if (raw === "" && !item.required) continue; if (item.control === "number") result[item.name] = Number(raw); else if (item.control === "switch") result[item.name] = raw === "true"; else if (item.control === "tags") result[item.name] = String(raw || "").split(",").map((value) => value.trim()).filter(Boolean); else if (item.control === "key_value") result[item.name] = raw ? JSON.parse(raw) : {}; else result[item.name] = raw; } return result; }
