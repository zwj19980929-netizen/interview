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
  const [fps, setFps] = useState(0);

  useEffect(() => {
    let active = true;
    let assetVerified = false;
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
    }).then((avatarConfig) => {
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
          setFps(value);
          onFps?.(value);
        },
        onProblem: (error) => {
          reportFatal(error, "AVATAR_RENDERER_FAILED");
        },
      });
    }).then((value) => {
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
    <canvas ref={canvasRef} aria-label="自研 3D 数字人面试官" />
    {status === "loading" && <div className="vrm-avatar-gate" role="status"><span className="spinner" /><strong>正在验证并加载专属 3D 面试官</strong><small>未通过资产与 WebGL 检查前不会开始正式面试</small></div>}
    {status === "pausing" && <div className="vrm-avatar-gate is-error" role="status"><strong>3D 数字人不可用，正在确认服务器暂停</strong><small>未确认前请停止作答。</small></div>}
    {status === "paused" && <div className="vrm-avatar-gate is-error" role="alert"><strong>3D 数字人不可用，面试已在服务器暂停</strong><small>系统不会退回静态图片或假口型，请等待企业面试官接管。</small></div>}
    {status === "failed" && <div className="vrm-avatar-gate is-error" role="alert"><strong>3D 数字人不可用，服务器暂停尚未确认</strong><small>请停止作答并联系企业面试官。</small></div>}
    <div className="candidate-stage-caption">
      <span><strong>自研实时 3D 面试官</strong><small>{status === "ready" ? `${Math.round(fps)} FPS · viseme 音画同步` : "正式表达链路检查中"}</small></span>
      <span className={`avatar-speaking-badge${avatar?.status === "speaking" ? " is-active" : ""}`}>{avatar?.status === "speaking" ? "正在说话" : "等待发言"}</span>
    </div>
  </div>;
}
