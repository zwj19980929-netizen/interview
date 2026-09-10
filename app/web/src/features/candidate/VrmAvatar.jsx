import { useEffect, useRef, useState } from "react";

import {
  createVrmAvatarRenderer,
  verifyLicensedVrmAsset,
} from "./vrm-avatar.js";

export function VrmAvatar({
  apiBase,
  interviewId,
  candidateSessionToken,
  avatar,
  onFatalProblem,
  onReady,
  onFps,
  verifyAsset = verifyLicensedVrmAsset,
  rendererFactory = createVrmAvatarRenderer,
}) {
  const canvasRef = useRef(null);
  const rendererRef = useRef(null);
  const [status, setStatus] = useState("loading");

  useEffect(() => {
    let active = true;
    let assetVerified = false;
    const controller = new AbortController();
    setStatus("loading");
    const reportFatal = async (error, fallbackCode) => {
      if (!active) return;
      const reported = error instanceof Error ? error : new Error(String(error));
      if (!reported.candidateProblemCode) reported.candidateProblemCode = fallbackCode;
      setStatus("pausing");
      let result = null;
      try {
        result = await onFatalProblem?.(reported);
      } catch { /* rendered below */ }
      if (active) setStatus(result?.pauseConfirmed ? "paused" : "failed");
    };
    verifyAsset({
      apiBase,
      interviewId,
      candidateSessionToken,
      signal: controller.signal,
    }).then((avatarConfig) => {
      if (!active) return null;
      assetVerified = true;
      return rendererFactory(canvasRef.current, {
        avatarConfig,
        onReady: (details) => {
          if (!active) return;
          setStatus("ready");
          onReady?.(details);
        },
        onFps: (value) => {
          if (!active) return;
          onFps?.(value);
        },
        onProblem: (error) => {
          reportFatal(error, "AVATAR_RENDERER_FAILED");
        },
      });
    }).then((value) => {
      if (!value) return;
      if (!active) value.dispose();
      else rendererRef.current = value;
    }).catch((error) => {
      if (!active) return;
      reportFatal(
        error,
        assetVerified ? "AVATAR_MODEL_LOAD_FAILED" : "AVATAR_ASSET_UNAVAILABLE",
      );
    });
    return () => {
      active = false;
      controller.abort();
      rendererRef.current?.dispose();
      rendererRef.current = null;
    };
  }, [
    apiBase,
    candidateSessionToken,
    interviewId,
    onFatalProblem,
    onFps,
    onReady,
    rendererFactory,
    verifyAsset,
  ]);

  useEffect(() => {
    rendererRef.current?.setPerformanceState({
      viseme: avatar?.viseme || "sil",
      visemeWeight: avatar?.visemeWeight || 0,
      gesture: avatar?.gesture || "idle",
      gestureIntensity: avatar?.gestureIntensity || 0,
      speaking: avatar?.status === "speaking",
    });
  }, [avatar]);

  return <div className={`candidate-avatar-stage vrm-avatar-stage is-${status}${avatar?.status === "speaking" ? " is-speaking" : ""}`}>
    <canvas ref={canvasRef} aria-label="面试官" />
    {status === "loading" && <div className="vrm-avatar-gate" role="status"><span className="spinner" /><strong>面试官正在准备</strong><small>请稍等片刻</small></div>}
    {status === "pausing" && <div className="vrm-avatar-gate is-error" role="status"><strong>正在暂停面试</strong><small>请先停止作答。</small></div>}
    {status === "paused" && <div className="vrm-avatar-gate is-error" role="alert"><strong>面试已暂停</strong><small>请联系面试安排人协助恢复。</small></div>}
    {status === "failed" && <div className="vrm-avatar-gate is-error" role="alert"><strong>连接中断，请先停止作答</strong><small>请联系面试安排人确认后再继续。</small></div>}
    <div className="candidate-stage-caption">
      <span><strong>面试官</strong></span>
      <span className={`avatar-speaking-badge${avatar?.status === "speaking" ? " is-active" : ""}`}>{avatar?.status === "speaking" ? "正在说话" : "等待发言"}</span>
    </div>
  </div>;
}
