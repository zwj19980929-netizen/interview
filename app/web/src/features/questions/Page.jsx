import { useEffect, useMemo, useRef, useState } from "react";

import { useWorkbench } from "../../core/WorkbenchProvider.jsx";
import { Empty, Field, ModalForm, Status, formatDate, splitComma, splitLines } from "../../core/ui.jsx";

export default function QuestionsPage() {
  const workbench = useWorkbench();
  return workbench.route.generation
    ? <QuestionGenerationWorkbench {...workbench} />
    : workbench.route.knowledgeBaseId
    ? <KnowledgeBaseDetail {...workbench} />
    : <KnowledgeBaseList {...workbench} />;
}

function KnowledgeBaseList({ API, data, request, navigate, reloadRoute, openModal, closeModal, toast }) {
  const create = () => openModal({
    title: "新建题库",
    body: <QuestionSetupForm positions={data.positions} onSubmit={async (form) => {
      let positionId = form.get("job_position_id");
      if (!positionId) {
        const position = await request(`${API}/job-positions`, {
          method: "POST",
          body: { code: form.get("position_code"), name: form.get("position_name"), description: form.get("position_description") },
        });
        positionId = position.id;
      }
      const knowledgeBase = await request(`${API}/job-positions/${encodeURIComponent(positionId)}/knowledge-bases`, {
        method: "POST",
        body: { name: form.get("knowledge_base_name"), description: form.get("knowledge_base_description"), language: "zh-CN", voice_profile_id: "voice_default_cn" },
      });
      closeModal();
      await reloadRoute("knowledgeBases", "positions");
      const speechProfile = knowledgeBase.speech_profile;
      const speechMessage = speechProfile?.source === "model_route_default"
        ? "已使用管理员配置的默认语音模型和声音；进入题库后仍可单独调整"
        : speechProfile?.source === "development_mock"
          ? "当前只有开发模拟语音，不能试听；请进入题库配置真实语音模型"
          : "尚未配置语音模型；进入题库后完成配置即可生成和试听读题语音";
      toast("题库已创建", speechMessage);
      navigate("questions", knowledgeBase.id);
    }} />,
  });

  return <>
    <section className="page-header"><div><h1>题库</h1><p>{data.knowledgeBases.length} 个题库 · 按题库维护题目和读题语音</p></div><button className="button button-primary" onClick={create}>新建题库</button></section>
    {data.knowledgeBases.length ? <div className="knowledge-base-grid">{data.knowledgeBases.map((item) => {
      const profile = item.speech_profile;
      const build = item.current_speech_build;
      return <button className="knowledge-base-card" type="button" key={item.id} onClick={() => navigate("questions", item.id)}>
        <span className="knowledge-base-card-head"><span><strong>{item.name}</strong><small>{item.job_position?.name || "未命名岗位"}</small></span><Status value={item.speech_build_status || item.status} /></span>
        <span className="knowledge-base-description">{item.description || "暂未填写题库说明"}</span>
        <span className="knowledge-base-metrics"><span><strong>{item.question_count || 0}</strong><small>题目</small></span><span><strong>{item.speech_ready_count || 0}</strong><small>语音就绪</small></span></span>
        <span className={`knowledge-base-speech ${profile ? "is-configured" : "is-unconfigured"}`}>
          <span className="knowledge-base-speech-copy">
            <small>读题语音</small>
            <strong>{profile?.voice_profile_id || "未配置"}</strong>
          </span>
          <span className="knowledge-base-speech-status">{profile ? "已配置" : "待配置"}</span>
        </span>
        {build && ["pending", "running", "failed"].includes(build.status) && <BuildProgress build={build} />}
      </button>;
    })}</div> : <Empty title="尚无题库" copy="新建题库后，再进入题库配置语音模型和维护题目" />}
  </>;
}

function KnowledgeBaseDetail({ API, data, request, navigate, reloadRoute, openModal, closeModal, toast }) {
  const knowledgeBase = data.selectedKnowledgeBase;
  const [query, setQuery] = useState("");
  const [playingId, setPlayingId] = useState("");
  const playerRef = useRef(null);
  const questions = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return data.questions;
    return data.questions.filter((item) => [item.title, item.question_text, ...(item.skills || [])].join(" ").toLowerCase().includes(needle));
  }, [data.questions, query]);
  if (!knowledgeBase) return <Empty title="题库不存在" copy="返回题库列表后重新选择" />;

  const finish = async (title, message) => {
    closeModal();
    await reloadRoute("knowledgeBases", "knowledgeBaseDetail", "questions", "speechBuilds", "speechOptions", "questionGenerationOptions", "questionGenerationBatches");
    toast(title, message);
  };
  const createQuestion = () => openModal({
    title: `向 ${knowledgeBase.name} 添加题目`,
    body: <QuestionForm onSubmit={async (form) => {
      const result = await request(`${API}/knowledge-bases/${encodeURIComponent(knowledgeBase.id)}/questions`, { method: "POST", body: questionPayload(form, knowledgeBase.id) });
      await finish("题目已创建", result.speech_configuration_required ? "请先配置题库 TTS，之后会生成读题语音" : "语音任务已交给 Celery worker");
    }} />,
  });
  const configureSpeech = () => openModal({
    title: "配置题库读题语音",
    body: <SpeechProfileForm knowledgeBase={knowledgeBase} speechOptions={data.speechOptions} onTest={async (modelId) => {
      try {
        await request(`${API}/admin/model-configurations/${encodeURIComponent(modelId)}/test`, { method: "POST", body: {}, timeoutMs: 40000 });
        const next = await request(`${API}/knowledge-bases/${encodeURIComponent(knowledgeBase.id)}/speech-options`);
        toast("TTS 模型测试通过", "现在可以保存该模型和声音");
        return next;
      } catch (error) {
        const next = await request(`${API}/knowledge-bases/${encodeURIComponent(knowledgeBase.id)}/speech-options`);
        const message = error.code === "provider_rate_limited"
          ? "智谱返回频控或额度不足，请检查账户余额/并发限制后重试"
          : error.code === "provider_auth_failed" ? "API Key 未通过智谱认证，请检查厂商连接凭据" : error.message;
        toast("TTS 模型测试失败", message, "error");
        return next;
      }
    }} onSubmit={async (value) => {
      const build = await request(`${API}/knowledge-bases/${encodeURIComponent(knowledgeBase.id)}/speech-profile`, {
        method: "PUT",
        idempotencyKey: globalThis.crypto?.randomUUID?.() || `${Date.now()}`,
        body: { expected_version: knowledgeBase.version, ...value },
      });
      await finish("语音配置已更新", `已提交 ${build.total} 道题目的整库语音重建`);
    }} />,
  });
  const details = (item) => openModal({
    title: item.title,
    body: <QuestionDetail item={item} />,
  });
  const editQuestion = (item) => openModal({
    title: `编辑 ${item.title}`,
    body: <QuestionForm current={item} onSubmit={async (form) => {
      await request(`${API}/questions/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        body: { expected_version: item.version, ...questionFormPayload(form) },
      });
      await finish("题目已更新", "题干变化时只会重新生成这一道题的语音");
    }} />,
  });
  const deleteQuestion = (item) => openModal({
    title: "删除题目",
    body: <ModalForm submitLabel="确认删除" submitVariant="danger" onSubmit={async () => {
      await request(`${API}/questions/${encodeURIComponent(item.id)}?expected_version=${item.version}`, { method: "DELETE" });
      await finish("题目已删除", "题目已从当前题库移除，历史面试记录和旧语音资产保持不变");
    }}><div className="delete-warning field-full"><strong>从当前题库移除“{item.title}”？</strong><p>该题不会再参与后续面试计划；历史面试快照不会被删除。</p></div></ModalForm>,
  });
  const previewQuestion = async (item) => {
    try {
      playerRef.current?.pause();
      const access = await request(`${API}/question-speech-assets/${encodeURIComponent(item.speech_asset_id)}/content-url`, { method: "POST" });
      const player = new Audio(access.url);
      playerRef.current = player;
      setPlayingId(item.id);
      player.onended = () => setPlayingId("");
      player.onerror = () => { setPlayingId(""); toast("试听失败", "浏览器无法播放该语音格式", "error"); };
      await player.play();
    } catch (error) {
      setPlayingId("");
      const message = error.code === "QUESTION_SPEECH_PREVIEW_UNAVAILABLE"
        ? "当前是开发模拟语音，没有实际音频。请点击页面顶部“配置语音”，选择已测试通过的语音模型和声音。"
        : error.code === "QUESTION_SPEECH_ASSET_NOT_PRIVATE"
          ? "语音文件尚未保存到系统私有存储。请重新生成；如果仍失败，请管理员检查文件存储配置。"
          : error.message;
      toast("暂时不能试听", message, "error");
    }
  };
  const profile = knowledgeBase.speech_profile;
  const developmentMock = profile?.source === "development_mock" || profile?.model_configuration_id?.startsWith("model_cfg_mock_");
  const selectedModel = data.speechOptions.items.find((item) => item.id === profile?.model_configuration_id);
  const currentBuild = data.speechBuilds[0];

  return <>
    <button className="back-link" type="button" onClick={() => navigate("questions")}>← 返回题库</button>
    <section className="page-header knowledge-base-detail-header"><div><h1>{knowledgeBase.name}</h1><p>{knowledgeBase.job_position_id} · {data.questions.length} 道题目</p></div><div className="page-actions"><button className="button button-secondary" onClick={configureSpeech}>配置语音</button><button className="button button-secondary generation-trigger" onClick={() => navigate("questions", `${knowledgeBase.id}/generation`)}>AI 智能生题</button><button className="button button-primary" onClick={createQuestion}>新建题目</button></div></section>
    <section className="speech-profile-panel">
      <div className="speech-profile-copy"><span className="eyebrow">题库读题语音</span><strong>{developmentMock ? "开发模拟语音（不可试听）" : selectedModel?.display_name || profile?.model_configuration_id || "尚未配置 TTS 模型"}</strong><span>{developmentMock ? "请选择已测试通过的真实语音模型和声音" : profile ? `${profile.voice_profile_id} · ${profile.language} · ${profile.audio_format} · ${profile.speaking_rate}x` : "选择模型和声音后，将为题库内所有题目生成语音"}</span></div>
      <Status value={developmentMock ? "configuration_required" : knowledgeBase.speech_build_status || "configuration_required"} />
      {currentBuild && <BuildProgress build={currentBuild} />}
    </section>
    {developmentMock && <div className="inline-warning">当前题库使用的是开发模拟语音，只用于流程测试，不包含实际音频。点击“配置语音”，选择已测试通过的模型和声音后，系统会重新生成全部题目语音。</div>}
    {!data.speechOptions.items.length && <div className="inline-warning">{data.speechOptions.candidates?.length ? `已找到 ${data.speechOptions.candidates.length} 个 TTS 模型，但尚未测试通过。点击“配置语音”可直接测试并启用。` : "还没有支持 TTS 的模型。请先到“模型服务”添加 TTS 模型。"}</div>}
    <section className="section-title-row question-list-heading"><div><h2>题目</h2><p>题目变化只生成当前题目的新语音；模型或声音变化会整库重建</p></div><input className="form-input question-filter" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索题目或技能" /></section>
    {questions.length ? <div className="data-table-wrap"><table className="data-table"><thead><tr><th>题目</th><th>难度</th><th>校验</th><th>语音</th><th></th></tr></thead><tbody>{questions.map((item) => {
      const speechStatus = !profile || developmentMock ? "configuration_required" : currentBuild && ["queued", "running"].includes(currentBuild.status) ? "rebuilding" : item.speech_status;
      const preview = item.speech_preview || {};
      const canPreview = speechStatus === "ready" && Boolean(item.speech_asset_id) && preview.available !== false;
      return <tr key={item.id}><td><strong>{item.title}</strong><small className="cell-subtitle">{item.skills?.join(" · ")}</small></td><td>{item.difficulty}</td><td><Status value={item.validation_status} /></td><td><Status value={speechStatus} />{!canPreview && preview.message && <small className="cell-subtitle">{preview.message}</small>}</td><td><div className="table-actions"><button className="button button-secondary button-small" onClick={() => previewQuestion(item)} disabled={!canPreview} title={canPreview ? "播放读题语音" : preview.message || "语音尚未准备好"}>{playingId === item.id ? "播放中…" : "试听"}</button><button className="button button-secondary button-small" onClick={() => details(item)}>查看</button><button className="button button-secondary button-small" onClick={() => editQuestion(item)}>编辑</button><button className="button button-danger button-small" onClick={() => deleteQuestion(item)}>删除</button></div></td></tr>;
    })}</tbody></table></div> : <Empty title={query ? "没有匹配题目" : "题库中还没有题目"} copy={query ? "换一个关键词试试" : "点击“新建题目”建立第一道题"} />}
  </>;
}

function QuestionGenerationWorkbench({ API, data, request, navigate, reloadRoute, openModal, closeModal, toast }) {
  const knowledgeBase = data.selectedKnowledgeBase;
  const batch = data.selectedGenerationBatch;
  const busy = batch && (["queued", "generating", "stopping", "importing"].includes(batch.status) || batch.drafts?.some((draft) => draft.import_status === "importing"));

  useEffect(() => {
    if (!busy) return undefined;
    const timer = window.setTimeout(() => {
      reloadRoute("questionGenerationBatches", "selectedGenerationBatch").catch(() => {});
    }, 2200);
    return () => window.clearTimeout(timer);
  }, [batch?.id, batch?.status, batch?.version, busy, reloadRoute]);

  if (!knowledgeBase) return <Empty title="题库不存在" copy="返回题库列表后重新选择" />;

  const refresh = async () => reloadRoute("questionGenerationBatches", "selectedGenerationBatch");
  const finish = async (title, message) => {
    await refresh();
    closeModal();
    toast(title, message);
  };
  const createBatch = async (value) => {
    const created = await request(`${API}/knowledge-bases/${encodeURIComponent(knowledgeBase.id)}/question-generation-batches`, {
      method: "POST",
      idempotencyKey: globalThis.crypto?.randomUUID?.() || `${Date.now()}`,
      body: value,
      timeoutMs: 15000,
    });
    closeModal();
    toast("智能生题已开始", `正在生成 ${created.target_count} 道候选题，可在这里停止或查看分片进度`);
    navigate("questions", `${knowledgeBase.id}/generation/${created.id}`);
  };
  const openCreate = () => openModal({
    title: "新建生题任务",
    body: <QuestionGenerationForm options={data.questionGenerationOptions} onSubmit={createBatch} />,
  });
  const command = (action, title, copy, { chunk = null, variant = "primary" } = {}) => openModal({
    title,
    body: <ModalForm submitLabel={title} submitVariant={variant} onSubmit={async () => {
      const endpoint = chunk
        ? `${API}/question-generation-batches/${encodeURIComponent(batch.id)}/chunks/${encodeURIComponent(chunk.chunk_id)}/retry`
        : `${API}/question-generation-batches/${encodeURIComponent(batch.id)}/${action}`;
      await request(endpoint, {
        method: "POST",
        idempotencyKey: globalThis.crypto?.randomUUID?.() || `${Date.now()}`,
        body: { expected_version: batch.version, reason: action === "stop" ? "面试官在生题工作台停止" : "面试官在生题工作台重试" },
      });
      await finish(title === "停止任务" ? "已提交停止" : "任务已重新排队", action === "stop" ? "系统不会再发起新的模型调用，在途调用的迟到结果也不会写入" : "已完成的分片会保留，只处理失败或未完成部分");
    }}><div className="generation-command-confirm field-full"><strong>{copy}</strong><p>{action === "stop" ? "已到达供应商的请求可能无法立即撤销，但停止后的结果不会进入候选题。" : "该操作可能产生新的模型调用费用，成功分片不会重复生成。"}</p></div></ModalForm>,
  });
  const editDraft = (draft) => openModal({
    title: `审核候选题：${draft.title}`,
    body: <QuestionForm current={draft} submitLabel="保存候选题" onSubmit={async (form) => {
      await request(`${API}/question-generation-batches/${encodeURIComponent(batch.id)}/drafts/${encodeURIComponent(draft.id)}`, {
        method: "PATCH",
        body: { expected_version: batch.version, ...questionFormPayload(form) },
      });
      await finish("候选题已保存", "修改仍只属于当前生成批次，尚未进入正式题库");
    }} />,
  });
  const deleteDraft = (draft) => openModal({
    title: "删除候选题",
    body: <ModalForm submitLabel="确认删除" submitVariant="danger" onSubmit={async () => {
      await request(`${API}/question-generation-batches/${encodeURIComponent(batch.id)}/drafts/${encodeURIComponent(draft.id)}?expected_version=${batch.version}`, { method: "DELETE" });
      await finish("候选题已删除", "正式题库没有受到影响");
    }}><div className="delete-warning field-full"><strong>删除候选题“{draft.title}”？</strong><p>删除后不会随本批次导入。</p></div></ModalForm>,
  });
  const viewDraft = (draft) => openModal({
    title: draft.title,
    body: <QuestionDetail item={draft} />,
  });
  const importDraft = (draft) => openModal({
    title: "单独导入题目",
    body: <ModalForm submitLabel={draft.import_status === "failed" ? "重新导入这一题" : "确认导入这一题"} onSubmit={async () => {
      await request(`${API}/question-generation-batches/${encodeURIComponent(batch.id)}/drafts/${encodeURIComponent(draft.id)}/import`, {
        method: "POST",
        idempotencyKey: globalThis.crypto?.randomUUID?.() || `${Date.now()}`,
        body: { expected_version: batch.version, expected_draft_version: draft.version || 1 },
      });
      await finish("已提交单题导入", "其他候选题仍保留在当前审核批次中");
    }}><div className="import-confirmation field-full"><strong>{draft.title}</strong><p>只把这一道候选题导入正式题库；其他题目不会受影响。导入成功后该候选题不能再次编辑或删除。</p></div></ModalForm>,
  });
  const importBatch = () => openModal({
    title: "确认导入题库",
    body: <ModalForm submitLabel={`导入 ${batch.drafts.length} 道题目`} onSubmit={async () => {
      await request(`${API}/question-generation-batches/${encodeURIComponent(batch.id)}/import`, {
        method: "POST",
        idempotencyKey: globalThis.crypto?.randomUUID?.() || `${Date.now()}`,
        body: { expected_version: batch.version },
      });
      await finish("已提交导入", "Celery 将写入正式题库，并按当前语音配置生成读题语音");
    }}><div className="import-confirmation field-full"><strong>审核完成后再导入</strong><p>候选题将成为正式题目，重复提交不会产生副本。</p></div></ModalForm>,
  });

  return <>
    <button className="back-link" type="button" onClick={() => navigate("questions", knowledgeBase.id)}>← 返回 {knowledgeBase.name}</button>
    <section className="page-header generation-workbench-header"><div><span className="eyebrow">智能生题工作台</span><h1>{knowledgeBase.name}</h1><p>任务执行、失败恢复和候选题审核都在这里完成</p></div><div className="page-actions"><button className="button button-secondary" onClick={refresh}>刷新任务</button><button className="button button-primary" onClick={openCreate}>新建生题任务</button></div></section>
    <div className="generation-workbench-grid">
      <aside className="generation-history-panel"><div className="section-title-row"><div><h2>任务历史</h2><p>{data.questionGenerationBatches.length} 个批次</p></div></div>{data.questionGenerationBatches.length ? <div className="generation-history-list">{data.questionGenerationBatches.map((item) => <button type="button" className={`generation-history-item${batch?.id === item.id ? " is-active" : ""}`} key={item.id} onClick={() => navigate("questions", `${knowledgeBase.id}/generation/${item.id}`)}><span><strong>{item.context_snapshot?.positioning || "智能生题任务"}</strong><Status value={item.status} /></span><small>{formatDate(item.created_at)} · 目标 {item.target_count} 道</small><span className="generation-history-progress">已接受 {item.generation_progress?.accepted_count || 0} / {item.target_count}</span></button>)}</div> : <Empty title="还没有生题任务" copy="填写上方配置创建第一个批次" />}</aside>
      <main className="generation-task-detail">{batch ? <QuestionGenerationBatchPanel
        batch={batch}
        onView={viewDraft}
        onEdit={editDraft}
        onDelete={deleteDraft}
        onImportDraft={importDraft}
        onImport={importBatch}
        onRefresh={refresh}
        onStop={() => command("stop", "停止任务", "确定停止当前生题任务？", { variant: "danger" })}
        onResume={() => command("resume", "继续未完成项", "继续这个批次尚未完成的题目？")}
        onRetryFailed={() => command("retry-failed", "重试全部失败项", "只重新执行失败的规划、分片或合并任务？")}
        onRetryChunk={(task) => command("retry", "重试这个分片", `重新生成 ${task.slot_ids.join("、")} 对应的题目？`, { chunk: task })}
      /> : <Empty title="选择一个任务查看详情" copy="左侧保留所有历史批次、失败原因和人工操作记录" />}</main>
    </div>
  </>;
}

function BuildProgress({ build }) {
  const total = Math.max(0, Number(build.total || 0));
  const ready = Math.max(0, Number(build.ready || 0));
  const percent = total ? Math.round((ready / total) * 100) : build.status === "ready" ? 100 : 0;
  return <span className="speech-build-progress"><span className="speech-build-track"><span style={{ width: `${percent}%` }} /></span><small>{build.status === "failed" ? `${build.failed} 道失败` : `${ready}/${total} 已生成`}</small></span>;
}

function SpeechProfileForm({ knowledgeBase, speechOptions, onTest, onSubmit }) {
  const current = knowledgeBase.speech_profile || {};
  const [catalog, setCatalog] = useState(speechOptions || { items: [], candidates: [] });
  const [testingId, setTestingId] = useState("");
  const options = catalog.items || [];
  const candidates = catalog.candidates || options;
  const initialModelId = candidates.some((item) => item.id === current.model_configuration_id) ? current.model_configuration_id : candidates[0]?.id || "";
  const [modelId, setModelId] = useState(initialModelId);
  const model = candidates.find((item) => item.id === modelId);
  const readyModel = options.find((item) => item.id === modelId);
  const initialVoice = model?.voices?.some((item) => item.voice_profile_id === current.voice_profile_id) ? current.voice_profile_id : model?.voices?.[0]?.voice_profile_id || "";
  const [voiceId, setVoiceId] = useState(initialVoice);
  const changeModel = (event) => {
    const id = event.target.value;
    const next = candidates.find((item) => item.id === id);
    setModelId(id);
    setVoiceId(next?.voices?.[0]?.voice_profile_id || "");
  };
  const testModel = async (id) => {
    setTestingId(id);
    try {
      const nextCatalog = await onTest(id);
      setCatalog(nextCatalog);
      const ready = nextCatalog.items?.find((item) => item.id === id) || nextCatalog.items?.[0];
      const selected = ready || nextCatalog.candidates?.find((item) => item.id === id) || nextCatalog.candidates?.[0];
      setModelId(selected?.id || "");
      setVoiceId(selected?.voices?.[0]?.voice_profile_id || "");
    } finally { setTestingId(""); }
  };
  return <ModalForm onSubmit={async (form) => onSubmit({ model_configuration_id: modelId, voice_profile_id: voiceId, language: form.get("language"), audio_format: form.get("audio_format"), speaking_rate: Number(form.get("speaking_rate")) })} submitLabel="保存并重新生成全部语音" submitDisabled={!readyModel || !voiceId}>
    <p className="delete-warning">切换模型、声音或输出参数后，该题库当前所有题目都会重新生成语音。历史面试使用的旧语音不会被覆盖。</p>
    {candidates.length ? <div className="tts-candidate-list field-full">{candidates.map((item) => <div className="tts-candidate" key={item.id}><span><strong>{item.display_name}</strong><small>{item.provider_model_id}</small></span><Status value={item.selectable ? "ready" : item.status || item.unavailable_reason} />{!item.selectable && item.enabled !== false && <button className="button button-secondary button-small" type="button" disabled={testingId === item.id} onClick={() => testModel(item.id)}>{testingId === item.id ? "测试中…" : "测试并启用"}</button>}</div>)}</div> : <div className="inline-warning field-full">没有找到支持 TTS 的模型，请先到“模型服务”添加。</div>}
    <Field label="TTS 模型" full><select className="form-select" value={modelId} onChange={changeModel} required disabled={!candidates.length}><option value="">选择 TTS 模型</option>{candidates.map((item) => <option value={item.id} key={item.id}>{item.display_name} · {item.provider_model_id}{item.selectable ? " · 已就绪" : ` · ${item.status === "failed" ? "测试失败" : item.status === "untested" ? "待测试" : "不可用"}`}</option>)}</select></Field>
    <Field label="声音"><select className="form-select" value={voiceId} onChange={(event) => setVoiceId(event.target.value)} required disabled={!model?.voices?.length}>{model?.voices?.map((item) => <option value={item.voice_profile_id} key={item.voice_profile_id}>{item.label}</option>)}</select></Field>
    <Field label="语言"><select className="form-select" name="language" defaultValue={current.language || "zh-CN"}><option value="zh-CN">中文</option><option value="en-US">English</option></select></Field>
    <Field label="音频格式"><select className="form-select" name="audio_format" defaultValue={current.audio_format || "audio/wav"}><option value="audio/wav">WAV</option><option value="audio/mpeg">MP3</option><option value="audio/opus">Opus</option></select></Field>
    <Field label="语速"><input className="form-input" name="speaking_rate" type="number" min="0.5" max="2" step="0.1" defaultValue={current.speaking_rate || 1} /></Field>
  </ModalForm>;
}

function QuestionSetupForm({ positions, onSubmit }) {
  return <ModalForm onSubmit={onSubmit} submitLabel="建立题库"><p className="form-intro">题库属于一个岗位；创建后进入详情配置语音模型和题目。</p>{positions.length ? <Field label="所属岗位" full><select className="form-select" name="job_position_id" required>{positions.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field> : <><Field label="岗位编码"><input className="form-input" name="position_code" placeholder="例如 backend-engineer" required /></Field><Field label="岗位名称"><input className="form-input" name="position_name" placeholder="例如后端工程师" required /></Field><Field label="岗位说明" full><textarea className="form-textarea form-textarea-compact" name="position_description" /></Field></>}<Field label="题库名称" full><input className="form-input" name="knowledge_base_name" placeholder="例如 Java 后端核心题库" required /></Field><Field label="题库说明" full><textarea className="form-textarea form-textarea-compact" name="knowledge_base_description" /></Field></ModalForm>;
}

function QuestionForm({ onSubmit, current = null, submitLabel = null }) {
  return <ModalForm onSubmit={onSubmit} submitLabel={submitLabel || (current ? "保存修改" : "保存并生成语音")}><Field label="标题" full><input className="form-input" name="title" defaultValue={current?.title || ""} required /></Field><Field label="难度"><select className="form-select" name="difficulty" defaultValue={current?.difficulty || "mid"}><option value="junior">初级</option><option value="mid">中级</option><option value="senior">高级</option><option value="expert">专家</option></select></Field><Field label="题型"><select className="form-select" name="type" defaultValue={current?.type || "open_ended"}><option value="open_ended">开放问答</option><option value="coding_discussion">代码讨论</option><option value="scenario">场景题</option><option value="behavioral">行为题</option></select></Field><Field label="实际题干" full><textarea className="form-textarea" name="question_text" defaultValue={current?.question_text || ""} required /></Field><Field label="标准答案" full><textarea className="form-textarea" name="standard_answer" defaultValue={current?.standard_answer || ""} required /></Field><Field label="关键点（每行一个）" full><textarea className="form-textarea" name="key_points" defaultValue={current?.key_points?.map((item) => item.text).join("\n") || ""} required /></Field><Field label="技能标签" full><input className="form-input" name="skills" defaultValue={current?.skills?.join(", ") || ""} placeholder="至少一个；用逗号分隔，例如 Java, 并发" required /></Field></ModalForm>;
}

function QuestionGenerationForm({ options, onSubmit }) {
  const readyModels = options.items || [];
  const candidates = options.candidates || readyModels;
  return <ModalForm onSubmit={async (form) => onSubmit({
    model_configuration_id: form.get("model_configuration_id"),
    target_count: Number(form.get("target_count")),
    positioning: form.get("positioning"),
    tags: splitComma(form.get("tags")),
    requirements: form.get("requirements"),
  })} submitLabel="开始生成候选题" submitDisabled={!readyModels.length}>
    <div className="generation-intro field-full"><span className="generation-spark">AI</span><div><strong>先生成候选题，再由你决定是否入库</strong><p>生成内容可以逐题修改或删除；确认导入前不会参与面试计划，也不会生成语音。</p></div></div>
    {readyModels.length ? <Field label="生题模型" full><select className="form-select" name="model_configuration_id" defaultValue={readyModels[0]?.id} required>{readyModels.map((item) => <option value={item.id} key={item.id}>{item.display_name} · {item.provider_model_id}</option>)}</select></Field> : <div className="inline-warning field-full">{candidates.length ? "已有结构化 LLM，但尚未测试就绪。请先到“模型服务”测试并启用。" : "还没有支持结构化输出的 LLM，请先到“模型服务”添加。"}</div>}
    <Field label="生成数量"><input className="form-input" name="target_count" type="number" min="1" max="30" defaultValue="10" required /></Field>
    <Field label="题库标签"><input className="form-input" name="tags" defaultValue={(options.tags || []).join(", ")} placeholder="例如 Python, 数据库, 并发" required /></Field>
    <Field label="题库定位" full><textarea className="form-textarea form-textarea-compact" name="positioning" defaultValue={options.positioning || ""} placeholder="这套题主要考察什么岗位、能力和场景" required /></Field>
    <Field label="我的额外要求（可选）" full><textarea className="form-textarea" name="requirements" placeholder="例如：避免背诵题；场景题占一半；重点考察线上排障" /></Field>
  </ModalForm>;
}

function QuestionGenerationBatchPanel({ batch, onView, onEdit, onDelete, onImportDraft, onImport, onRefresh, onStop, onResume, onRetryFailed, onRetryChunk }) {
  const busy = ["queued", "generating", "stopping", "importing"].includes(batch.status);
  const progress = batch.generation_progress || {};
  const phaseCopy = {
    queued: "等待 Celery 调度",
    planning: `正在规划 ${progress.target_count || batch.target_count || 0} 个互不重复的题目方向`,
    generating: `正在并行生成题目 · ${progress.completed_chunks || 0}/${progress.total_chunks || 0} 个子任务完成`,
    merging: "正在校验格式、合并并去除重复题目",
    refilling: `正在补生成被判定为重复或不合格的题目 · 第 ${progress.refill_round || 1} 轮`,
    stopping: "正在停止任务并等待在途 Worker 安全收尾",
    stopped: "任务已停止，可继续未完成项",
    reviewing: "候选题已生成，等待人工审核",
    failed: "生成任务失败",
  }[progress.phase || batch.phase];
  const statusCopy = {
    queued: "等待 Celery 调度",
    generating: "模型正在生成候选题",
    stopping: "正在停止",
    stopped: "任务已停止",
    reviewing: `待审核 · ${batch.drafts?.length || 0} 道`,
    importing: "正在导入正式题库并创建语音任务",
    imported: `已导入 ${batch.imported_question_ids?.length || 0} 道`,
    failed: "任务失败",
  }[batch.status] || batch.status;
  const failedTasks = (batch.tasks || []).filter((task) => task.error || ["failed", "dead_letter"].includes(task.work_status));
  return <section className={`generation-panel generation-${batch.status}`}>
    <div className="generation-panel-head"><div><span className="eyebrow">生题批次 · 执行版本 {batch.execution_revision || 1}</span><h2>{statusCopy}</h2><p>{batch.context_snapshot?.positioning}</p><small>{batch.context_snapshot?.tags?.join(" · ")} · {formatDate(batch.created_at)}</small></div><div className="generation-panel-actions"><Status value={batch.status} /><button className="button button-secondary button-small" type="button" onClick={onRefresh}>{busy ? "刷新进度" : "刷新"}</button>{batch.available_actions?.includes("stop") && <button className="button button-danger button-small" type="button" onClick={onStop}>停止</button>}{batch.available_actions?.includes("resume") && <button className="button button-primary button-small" type="button" onClick={onResume}>继续未完成项</button>}{batch.available_actions?.includes("retry_failed") && <button className="button button-primary button-small" type="button" onClick={onRetryFailed}>重试失败项</button>}{batch.available_actions?.includes("import") && <button className="button button-primary button-small" type="button" disabled={!batch.drafts?.length} onClick={onImport}>确认导入题库</button>}</div></div>
    {batch.status === "reviewing" && <section className="generation-review-section generation-review-primary"><div className="section-title-row"><div><h3>候选题审核</h3><p>点击题目区域查看完整内容；可逐题导入，也可修改或删除后批量导入</p></div><span>{batch.drafts?.length || 0} 道</span></div><div className="generation-draft-list">{batch.drafts.map((draft, index) => {
      const frozen = ["importing", "imported"].includes(draft.import_status);
      return <article className="generation-draft generation-draft-clickable" key={draft.id} role="button" tabIndex={0} onClick={() => onView(draft)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onView(draft); } }}><span className="generation-draft-index">{String(index + 1).padStart(2, "0")}</span><div className="generation-draft-copy"><div><strong>{draft.title}</strong><span>{draft.difficulty} · {draft.skills?.join(" / ")}</span>{draft.import_status && draft.import_status !== "pending" && <Status value={draft.import_status} />}</div><p>{draft.question_text}</p><small>{draft.key_points?.length || 0} 个评分关键点 · 点击查看完整题目</small>{draft.import_error && <small className="task-error-copy">{draft.import_error}</small>}</div><div className="table-actions" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}><button className="button button-primary button-small" type="button" disabled={frozen} onClick={() => onImportDraft(draft)}>{draft.import_status === "importing" ? "导入中…" : draft.import_status === "imported" ? "已导入" : draft.import_status === "failed" ? "重新导入" : "单独导入"}</button><button className="button button-secondary button-small" type="button" disabled={frozen} onClick={() => onEdit(draft)}>编辑</button><button className="button button-danger button-small" type="button" disabled={frozen} onClick={() => onDelete(draft)}>删除</button></div></article>;
    })}</div></section>}
    {busy && <div className="generation-progress" aria-live="polite">
      <div className="generation-busy"><span className="spinner" /><span>{phaseCopy || statusCopy}，页面会自动刷新</span></div>
      {progress.total_chunks > 0 && <div className="generation-progress-track" role="progressbar" aria-label="智能生题子任务进度" aria-valuemin="0" aria-valuemax={progress.total_chunks} aria-valuenow={progress.completed_chunks || 0}><span style={{ width: `${Math.min(100, ((progress.completed_chunks || 0) / progress.total_chunks) * 100)}%` }} /></div>}
      {progress.planned_count > 0 && <small>已规划 {progress.planned_count} 个方向 · 已接受 {progress.accepted_count || 0} 道 · 已过滤 {progress.rejected_count || 0} 道</small>}
    </div>}
    {batch.generation_warning && <div className="inline-warning">{batch.generation_warning}</div>}
    {batch.last_error && <div className="inline-error"><strong>处理失败</strong><span>{batch.last_error}</span></div>}
    {failedTasks.length > 0 && <section className="generation-failure-section"><div className="section-title-row"><div><h3>需要处理的失败项</h3><p>这里只显示失败原因和可执行的恢复操作</p></div><span>{failedTasks.length} 项</span></div><div className="generation-failure-list">{failedTasks.map((task) => <div className="generation-failure-item" key={task.id}><div><strong>{task.slot_ids?.length ? `题目分片 ${task.slot_ids.join("、")}` : task.type === "planning" ? "题目方向规划" : task.type === "merge" ? "候选题合并" : "题目导入"}</strong><small>{task.error?.code || "work_failed"} · {task.error?.message || "任务执行失败"}</small></div>{task.retryable && batch.status === "failed" && task.chunk_id && <button className="button button-secondary button-small" type="button" onClick={() => onRetryChunk(task)}>重试此分片</button>}</div>)}</div></section>}
    {batch.control_history?.length > 0 && <section className="generation-control-history"><h3>人工操作记录</h3>{[...batch.control_history].reverse().map((event, index) => <div key={`${event.created_at}-${index}`}><span>{event.action.startsWith("retry_chunk") ? "重试分片" : { stop: "停止任务", resume: "继续任务", retry_failed: "重试失败项" }[event.action] || event.action}</span><small>{event.actor_id} · {formatDate(event.created_at)} · revision {event.execution_revision}</small></div>)}</section>}
  </section>;
}

function QuestionDetail({ item }) {
  return <div className="question-detail"><div className="question-detail-meta"><Status value={item.import_status || item.validation_status || "valid"} /><span>{item.difficulty} · {item.type}</span><span>{item.skills?.join(" · ")}</span></div><Field label="题目" full><p className="evaluation-copy">{item.question_text}</p></Field><Field label="标准答案" full><p className="evaluation-copy">{item.standard_answer}</p></Field><h3>评分关键点</h3><ul>{item.key_points?.map((point) => <li key={point.id || point.text}><span>{point.text}</span>{point.weight != null && <small>权重 {point.weight}</small>}</li>)}</ul></div>;
}

function questionPayload(form, knowledgeBaseId) {
  return { knowledge_base_id: knowledgeBaseId, ...questionFormPayload(form) };
}

function questionFormPayload(form) {
  return { title: form.get("title"), question_text: form.get("question_text"), standard_answer: form.get("standard_answer"), key_points: splitLines(form.get("key_points")).map((text) => ({ text, weight: 1 })), skills: splitComma(form.get("skills")), difficulty: form.get("difficulty"), type: form.get("type"), rubric: { semantic_weight: 0.45, key_point_weight: 0.35, communication_weight: 0.2 } };
}
