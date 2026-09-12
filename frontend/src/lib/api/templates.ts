import { request } from "./client";

/** The full template config - same shape the backend's CourseTemplate schema
 * validates (sections/theme/block_styles/etc). Treated as an opaque JSON
 * document here rather than fully typed - the admin editor edits it as JSON,
 * matching how deep/nested this schema is. */
export type TemplateConfig = Record<string, unknown>;

export interface TemplateRecord {
  id: string;
  slug: string;
  version: number;
  template_id: string;
  name: string;
  kind: "technical" | "non_technical";
  description: string;
  is_current: boolean;
  is_archived: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  config: TemplateConfig;
}

export interface TemplateListResponse {
  templates: TemplateRecord[];
  total: number;
  limit: number;
  offset: number;
}

export interface TemplateCreateRequest {
  name: string;
  kind: "technical" | "non_technical";
  description?: string;
  config: TemplateConfig;
}

export interface TemplateUpdateRequest {
  name?: string;
  description?: string;
  config?: TemplateConfig;
}

export function listAdminTemplates(
  options: { include_archived?: boolean; limit?: number; offset?: number } = {},
): Promise<TemplateListResponse> {
  return request<TemplateListResponse>("/admin/templates", { query: { ...options } });
}

export function getAdminTemplate(slug: string): Promise<TemplateRecord> {
  return request<TemplateRecord>(`/admin/templates/${slug}`);
}

/** Full config for any resolvable template id (static or DB-backed) - used
 * to seed a new template/version from an existing one. */
export function getTemplateConfig(templateId: string): Promise<TemplateConfig> {
  return request<TemplateConfig>(`/admin/templates/config/${templateId}`);
}

export function listAdminTemplateVersions(slug: string): Promise<TemplateRecord[]> {
  return request<TemplateRecord[]>(`/admin/templates/${slug}/versions`);
}

export function createAdminTemplate(payload: TemplateCreateRequest): Promise<TemplateRecord> {
  return request<TemplateRecord>("/admin/templates", { method: "POST", body: payload });
}

export function updateAdminTemplate(
  slug: string,
  payload: TemplateUpdateRequest,
): Promise<TemplateRecord> {
  return request<TemplateRecord>(`/admin/templates/${slug}`, { method: "PATCH", body: payload });
}

export function archiveAdminTemplate(slug: string): Promise<TemplateRecord> {
  return request<TemplateRecord>(`/admin/templates/${slug}/archive`, { method: "POST" });
}

export function unarchiveAdminTemplate(slug: string): Promise<TemplateRecord> {
  return request<TemplateRecord>(`/admin/templates/${slug}/unarchive`, { method: "POST" });
}
