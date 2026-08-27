const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");

export const API_BASE = `${basePath}/api`;
export const APP_BASE = basePath;

export function appPath(path = ""): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${APP_BASE}${normalized}` || "/";
}
