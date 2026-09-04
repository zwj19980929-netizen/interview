import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

export class AvatarAssetError extends Error {
  constructor(message, {
    code = "AVATAR_ASSET_CONTRACT_INVALID",
    candidateProblemCode = "AVATAR_ASSET_UNAVAILABLE",
    status = 0,
  } = {}) {
    super(message);
    this.name = "AvatarAssetError";
    this.code = code;
    this.candidateProblemCode = candidateProblemCode;
    this.status = status;
  }
}

export async function verifyLicensedVrmAsset({
  apiBase = "/api/v1",
  interviewId,
  candidateSessionToken,
  configUrl,
  fetchImpl = globalThis.fetch,
} = {}) {
  if (!fetchImpl) throw new AvatarAssetError("无法验证自研 3D 数字人资产");
  if (!interviewId || !candidateSessionToken) {
    throw new AvatarAssetError("数字人资产缺少候选人会话授权", {
      code: "CANDIDATE_SESSION_TOKEN_MISSING",
      candidateProblemCode: "CANDIDATE_RUNTIME_FAILED",
    });
  }
  const scopedConfigUrl = configUrl || `${apiBase}/public/interviews/${encodeURIComponent(interviewId)}/avatar-config`;
  const response = await fetchImpl(scopedConfigUrl, {
    cache: "no-store",
    credentials: "same-origin",
    headers: { "X-Candidate-Session-Token": candidateSessionToken },
  });
  if (!response.ok) {
    let payload = null;
    try { payload = await response.json(); } catch { /* stable fallback below */ }
    const serverCode = payload?.error?.code || "AVATAR_ASSET_REQUEST_FAILED";
    throw avatarAssetRequestError(serverCode, response.status);
  }
  let config;
  try {
    config = await response.json();
  } catch {
    throw new AvatarAssetError("数字人资产合同不可解析");
  }
  const visemes = Array.isArray(config?.visemes) ? config.visemes : [];
  if (
    config?.ready !== true
    || config?.renderer !== "three-vrm-local"
    || config?.vrm_spec !== "1.0"
    || !String(config?.asset_url || "").startsWith("/")
    || !/^[a-f0-9]{64}$/i.test(String(config?.asset_sha256 || ""))
    || Number(config?.asset_grant_expires_at || 0) * 1000 <= Date.now()
    || visemes.length < 15
    || Number(config?.minimum_fps || 0) < 30
    || config?.amplitude_lipsync_formal !== false
  ) {
    throw new AvatarAssetError("专属 VRM 1.0 数字人或授权校验未就绪");
  }
  return Object.freeze({ ...config, visemes: Object.freeze([...visemes]) });
}

function avatarAssetRequestError(code, status) {
  const messages = {
    LICENSED_VRM_NOT_READY: "专属 3D 数字人资产尚未通过授权或完整性校验",
    AVATAR_ASSET_SESSION_NOT_ACTIVE: "当前面试状态不允许加载 3D 数字人",
    CANDIDATE_SESSION_TOKEN_INVALID: "候选人会话授权已失效",
    AVATAR_ASSET_CANDIDATE_BINDING_INVALID: "面试与数字人的候选人绑定不完整",
  };
  return new AvatarAssetError(messages[code] || "无法取得经授权的 3D 数字人资产", {
    code,
    candidateProblemCode: code === "CANDIDATE_SESSION_TOKEN_INVALID"
      ? "CANDIDATE_RUNTIME_FAILED"
      : "AVATAR_ASSET_UNAVAILABLE",
    status,
  });
}

export async function createVrmAvatarRenderer(canvas, {
  avatarConfig,
  onReady = () => {},
  onProblem = () => {},
  onFps = () => {},
} = {}) {
  if (!canvas) throw new Error("数字人画布不可用");
  const config = avatarConfig || await verifyLicensedVrmAsset();
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: false,
    powerPreference: "high-performance",
  });
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0xe7ece8, 1);

  const scene = new THREE.Scene();
  // The formal interview composition is a chest-up presenter shot. VRM files
  // are commonly exported in a T-pose, so keeping the generic GLTF camera
  // would make a technically valid asset look unfinished.
  const camera = new THREE.PerspectiveCamera(26, 16 / 9, 0.1, 20);
  camera.position.set(0, 1.5, 1.55);
  camera.lookAt(0, 1.47, 0);
  const hemisphere = new THREE.HemisphereLight(0xffffff, 0x718078, 2.3);
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(1.4, 2.2, 2.6);
  const fill = new THREE.DirectionalLight(0xbad7c6, 1.2);
  fill.position.set(-1.8, 1.3, 1.4);
  scene.add(hemisphere, key, fill);

  let vrm = null;
  let lookAtTarget = null;
  let frameId = 0;
  let disposed = false;
  let fatalReported = false;
  let lastFrameAt = performance.now();
  let fpsWindowAt = lastFrameAt;
  let fpsFrames = 0;
  let slowWindows = 0;
  let healthyWindows = 0;
  let readyReported = false;
  let lastBlinkAt = lastFrameAt;
  let nextBlinkDelay = 2600;
  let visual = {
    viseme: "sil",
    visemeWeight: 0,
    gesture: "idle",
    gestureIntensity: 0,
    speaking: false,
  };
  let lastViseme = "sil";

  const fail = (error) => {
    if (fatalReported || disposed) return;
    fatalReported = true;
    onProblem(error instanceof Error ? error : new Error(String(error)));
  };
  const contextLost = (event) => {
    event.preventDefault();
    fail(new Error("WebGL 上下文丢失，面试已暂停并等待人工接管"));
  };
  canvas.addEventListener("webglcontextlost", contextLost);

  try {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    const gltf = await loader.loadAsync(config.asset_url);
    vrm = gltf.userData.vrm;
    if (!vrm || String(vrm.meta?.metaVersion || "1") !== "1") {
      throw new Error("模型不是有效的 VRM 1.0 资产");
    }
    validateVisemeExpressions(vrm, config.visemes);
    if (!hasExpression(vrm.expressionManager, "blink")) {
      throw new Error("模型缺少正式 blink 表情");
    }
    if (!vrm.lookAt) {
      throw new Error("模型缺少正式 lookAt 注视控制");
    }
    VRMUtils.removeUnnecessaryVertices(gltf.scene);
    VRMUtils.combineSkeletons(gltf.scene);
    VRMUtils.rotateVRM0(vrm);
    scene.add(vrm.scene);
    vrm.scene.position.set(0, 0, 0);
    lookAtTarget = new THREE.Object3D();
    lookAtTarget.position.copy(camera.position);
    scene.add(lookAtTarget);
    vrm.lookAt.target = lookAtTarget;
    vrm.lookAt.autoUpdate = true;
  } catch (error) {
    renderer.dispose();
    canvas.removeEventListener("webglcontextlost", contextLost);
    throw new Error(`专属 3D 数字人加载失败：${error.message || error}`);
  }

  const resize = () => {
    const width = Math.max(1, canvas.clientWidth || canvas.width || 1);
    const height = Math.max(1, canvas.clientHeight || canvas.height || 1);
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    const targetWidth = Math.floor(width * pixelRatio);
    const targetHeight = Math.floor(height * pixelRatio);
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    }
  };

  const animate = (timestamp) => {
    if (disposed || fatalReported) return;
    resize();
    const delta = Math.min(0.05, Math.max(0, (timestamp - lastFrameAt) / 1000));
    lastFrameAt = timestamp;
    applyViseme(vrm, lastViseme, 0);
    applyViseme(vrm, visual.viseme, visual.visemeWeight);
    lastViseme = visual.viseme;
    applyBlink(vrm, timestamp, {
      lastBlinkAt,
      nextBlinkDelay,
      update(at, delay) { lastBlinkAt = at; nextBlinkDelay = delay; },
    });
    applyGesture(vrm, visual, timestamp);
    vrm.update(delta);
    renderer.render(scene, camera);
    fpsFrames += 1;
    if (timestamp - fpsWindowAt >= 1000) {
      const fps = (fpsFrames * 1000) / (timestamp - fpsWindowAt);
      onFps(fps);
      const gate = advanceAvatarFpsGate(
        { slowWindows, healthyWindows },
        fps,
        Number(config.minimum_fps),
      );
      slowWindows = gate.slowWindows;
      healthyWindows = gate.healthyWindows;
      if (slowWindows >= 3) {
        fail(new Error(`数字人持续帧率仅 ${Math.round(fps)} FPS，面试已暂停并等待人工接管`));
      }
      if (!fatalReported && !readyReported && healthyWindows >= 3) {
        readyReported = true;
        onReady({ assetUrl: config.asset_url, sha256: config.asset_sha256, fps });
      }
      fpsFrames = 0;
      fpsWindowAt = timestamp;
    }
    if (!fatalReported) frameId = requestAnimationFrame(animate);
  };
  frameId = requestAnimationFrame(animate);

  return {
    setPerformanceState(next) {
      visual = { ...visual, ...next };
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelAnimationFrame(frameId);
      canvas.removeEventListener("webglcontextlost", contextLost);
      if (vrm) {
        scene.remove(vrm.scene);
        VRMUtils.deepDispose(vrm.scene);
      }
      if (lookAtTarget) scene.remove(lookAtTarget);
      renderer.dispose();
    },
  };
}

export function advanceAvatarFpsGate(state, fps, minimumFps = 30) {
  const healthy = Number.isFinite(fps) && Math.round(fps) >= minimumFps;
  const slowWindows = healthy ? 0 : Number(state?.slowWindows || 0) + 1;
  const healthyWindows = healthy ? Number(state?.healthyWindows || 0) + 1 : 0;
  return Object.freeze({
    slowWindows,
    healthyWindows,
    ready: healthyWindows >= 3,
    failed: slowWindows >= 3,
  });
}

function validateVisemeExpressions(vrm, names) {
  const manager = vrm?.expressionManager;
  if (!manager) throw new Error("模型缺少 VRM 表情管理器");
  const missing = names
    .filter((name) => !hasExpression(manager, name) && !hasExpression(manager, `viseme_${name}`));
  if (missing.length) throw new Error(`模型缺少正式 viseme：${missing.join(", ")}`);
}

function hasExpression(manager, name) {
  try {
    if (typeof manager.getExpression === "function") return Boolean(manager.getExpression(name));
    return Boolean(manager.expressionMap?.[name]);
  } catch {
    return false;
  }
}

function applyViseme(vrm, shape, weight) {
  const manager = vrm?.expressionManager;
  if (!manager) return;
  const normalized = shape === "sil" ? 0 : Math.max(0, Math.min(1, Number(weight || 0)));
  for (const name of [shape, `viseme_${shape}`]) {
    if (hasExpression(manager, name)) manager.setValue(name, normalized);
  }
}

function applyBlink(vrm, timestamp, state) {
  const manager = vrm?.expressionManager;
  if (!manager) return;
  const elapsed = timestamp - state.lastBlinkAt;
  if (elapsed > state.nextBlinkDelay + 180) {
    state.update(timestamp, 2200 + Math.random() * 1800);
    setExpression(manager, "blink", 0);
    return;
  }
  if (elapsed <= state.nextBlinkDelay) return;
  const progress = (elapsed - state.nextBlinkDelay) / 180;
  setExpression(manager, "blink", Math.sin(Math.PI * Math.min(1, progress)));
}

function setExpression(manager, name, value) {
  if (hasExpression(manager, name)) manager.setValue(name, Math.max(0, Math.min(1, value)));
}

function applyGesture(vrm, visual, timestamp) {
  const humanoid = vrm?.humanoid;
  if (!humanoid) return;
  applyInterviewPresentationPose(humanoid, visual, timestamp);
  const head = humanoid.getNormalizedBoneNode("head");
  const chest = humanoid.getNormalizedBoneNode("chest") || humanoid.getNormalizedBoneNode("spine");
  const intensity = Math.max(0, Math.min(1, Number(visual.gestureIntensity || 0)));
  const breathe = Math.sin(timestamp / 850) * 0.012;
  if (chest) chest.rotation.x = breathe * (0.5 + intensity * 0.5);
  if (head) {
    const nod = visual.gesture === "nod" ? Math.sin(timestamp / 105) * 0.055 * intensity : 0;
    const listen = ["listen", "listening"].includes(visual.gesture) ? -0.035 * intensity : 0;
    const think = ["think", "thinking"].includes(visual.gesture) ? 0.045 * intensity : 0;
    const interrupt = visual.gesture === "interrupt" ? -0.025 * intensity : 0;
    head.rotation.x = nod + think + interrupt;
    head.rotation.y = Math.sin(timestamp / 2300) * 0.008;
    head.rotation.z = listen;
  }
}

export function applyInterviewPresentationPose(humanoid, visual = {}, timestamp = 0) {
  if (!humanoid?.getNormalizedBoneNode) return false;
  const leftUpperArm = humanoid.getNormalizedBoneNode("leftUpperArm");
  const rightUpperArm = humanoid.getNormalizedBoneNode("rightUpperArm");
  const leftLowerArm = humanoid.getNormalizedBoneNode("leftLowerArm");
  const rightLowerArm = humanoid.getNormalizedBoneNode("rightLowerArm");
  const intensity = Math.max(0, Math.min(1, Number(visual.gestureIntensity || 0)));
  const conversationalMotion = visual.speaking
    ? Math.sin(Number(timestamp || 0) / 420) * 0.035 * (0.4 + intensity * 0.6)
    : 0;

  // Normalized VRM rest bones point both arms horizontally. Rotate them down
  // into a restrained, symmetric presenter pose while leaving small head and
  // chest gestures to the normal performance timeline.
  if (leftUpperArm) {
    leftUpperArm.rotation.x = 0.04 + conversationalMotion;
    leftUpperArm.rotation.y = 0;
    leftUpperArm.rotation.z = -1.12;
  }
  if (rightUpperArm) {
    rightUpperArm.rotation.x = 0.04 - conversationalMotion;
    rightUpperArm.rotation.y = 0;
    rightUpperArm.rotation.z = 1.12;
  }
  if (leftLowerArm) {
    leftLowerArm.rotation.x = 0;
    leftLowerArm.rotation.y = 0;
    leftLowerArm.rotation.z = 0.12;
  }
  if (rightLowerArm) {
    rightLowerArm.rotation.x = 0;
    rightLowerArm.rotation.y = 0;
    rightLowerArm.rotation.z = -0.12;
  }
  return Boolean(leftUpperArm && rightUpperArm);
}
