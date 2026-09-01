import { HttpError } from "./http.js";

const DEFAULT_CONFLICT_CODES = new Set([
  "PERSISTENCE_CONFLICT",
  "RESUME_REVIEW_VERSION_CONFLICT",
]);

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  }
  return value;
}

export function semanticIdentity(fields) {
  return (resource) => fields.map((field) => canonical(resource?.[field]));
}

export function configurationIdentity(fallbackFields) {
  const fallback = semanticIdentity(fallbackFields);
  return (resource) => resource?.configuration_revision == null
    ? fallback(resource)
    : [resource.configuration_revision];
}

export async function requestWithLatestVersion({
  request,
  resourcePath,
  snapshot,
  identity,
  perform,
  changedMessage = "资源已被其他操作修改，已保留最新状态，请确认后重试",
  conflictCodes = DEFAULT_CONFLICT_CODES,
}) {
  const expectedIdentity = JSON.stringify(canonical(identity(snapshot)));
  const readLatest = async () => {
    const latest = await request(resourcePath);
    if (JSON.stringify(canonical(identity(latest))) !== expectedIdentity) {
      throw new HttpError(changedMessage, { status: 409, code: "RESOURCE_SEMANTIC_CONFLICT" });
    }
    return latest;
  };

  let latest = await readLatest();
  try {
    return await perform(latest);
  } catch (error) {
    if (!conflictCodes.has(error?.code)) throw error;
    latest = await readLatest();
    return perform(latest);
  }
}
