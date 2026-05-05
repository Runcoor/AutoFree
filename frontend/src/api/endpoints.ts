import { api } from './client'

// ─── auth ───────────────────────────────────────────────────
export const authApi = {
  me: () => api.get<{ authenticated: boolean; user_id: number | null }>('/auth/me').then(r => r.data),
  login: (password: string) => api.post<{ ok: true }>('/auth/login', { password }).then(r => r.data),
  logout: () => api.post<{ ok: true }>('/auth/logout').then(r => r.data),
  changePassword: (old: string, new_: string) =>
    api.post<{ ok: true; msg: string }>('/auth/change-password', { old, new: new_ }).then(r => r.data),
}

// ─── settings ───────────────────────────────────────────────
export interface CloudMailCfg { base_url: string; password_masked: string; has_password: boolean; msg?: string }
export interface SmsCfg {
  provider: string; api_key_masked: string; has_api_key: boolean
  service: string; country: string; operator: string; msg?: string
}
export interface CpaCfg { url: string; key_masked: string; has_key: boolean; enabled: boolean; msg?: string }

export const settingsApi = {
  getCloudMail: () => api.get<CloudMailCfg>('/settings/cloud-mail').then(r => r.data),
  putCloudMail: (body: Partial<{ base_url: string; password: string }>) =>
    api.put<CloudMailCfg>('/settings/cloud-mail', body).then(r => r.data),
  getSms: () => api.get<SmsCfg>('/settings/sms').then(r => r.data),
  putSms: (body: Partial<{ provider: string; api_key: string; service: string; country: string; operator: string }>) =>
    api.put<SmsCfg>('/settings/sms', body).then(r => r.data),
  smsBalance: () => api.post<{ provider: string; balance: number; currency: string; raw?: any }>('/settings/sms/balance').then(r => r.data),
  getCpa: () => api.get<CpaCfg>('/settings/cpa').then(r => r.data),
  putCpa: (body: Partial<{ url: string; key: string; enabled: boolean }>) =>
    api.put<CpaCfg>('/settings/cpa', body).then(r => r.data),
}

// ─── domains ────────────────────────────────────────────────
export interface Domain {
  id: number; domain: string; enabled: boolean
  success_count: number; fail_count: number
  last_used_at: string | null; created_at: string | null
}

export const domainsApi = {
  list: () => api.get<{ items: Domain[] }>('/domains').then(r => r.data.items),
  add: (domain: string) => api.post<Domain>('/domains', { domain }).then(r => r.data),
  toggle: (id: number, enabled: boolean) => api.patch<Domain>(`/domains/${id}`, { enabled }).then(r => r.data),
  remove: (id: number) => api.delete(`/domains/${id}`).then(() => null),
}

// ─── freegen ────────────────────────────────────────────────
export interface FreegenStatus {
  task_id?: string; batch_id?: string
  stage?: string; index?: number; total?: number
  ok?: number; failed?: number; current_email?: string
  events?: { ts: number; stage: string; [k: string]: any }[]
  started_at?: number
}
export interface Batch {
  id: string; domain: string; count: number; status: string
  started_at: string | null; finished_at: string | null
  ok: number; failed: number; created_at: string | null
}

export const freegenApi = {
  start: (count: number, domain?: string) =>
    api.post<{ task_id: string; batch_id: string; domain: string; count: number }>('/freegen/start',
      { count, domain }).then(r => r.data),
  stop: () => api.post<{ ok: true; msg: string }>('/freegen/stop').then(r => r.data),
  status: () => api.get<FreegenStatus>('/freegen/status').then(r => r.data),
  batches: (limit = 20) => api.get<{ items: Batch[] }>('/freegen/batches', { params: { limit } }).then(r => r.data.items),
}

// ─── accounts ───────────────────────────────────────────────
export interface Account {
  id: number; batch_id: string; email: string; password: string
  account_id: string; plan_type: string
  expires_at: string | null
  auth_json_path: string
  cpa_synced: boolean; cpa_synced_at: string | null; cpa_error: string | null
  created_at: string | null
}
export interface PendingAccount {
  id: number; batch_id: string; email: string; password: string
  error_kind: string; error: string
  created_at: string | null; resolved_at: string | null; resolved_via: string | null
}

export const accountsApi = {
  list: (params: { page?: number; page_size?: number; batch_id?: string; cpa_synced?: boolean } = {}) =>
    api.get<{ page: number; page_size: number; total: number; items: Account[] }>('/accounts', { params })
      .then(r => r.data),
  download: (email: string) => `/api/accounts/${encodeURIComponent(email)}/auth.json`,
  pending: () => api.get<{ items: PendingAccount[] }>('/accounts/pending').then(r => r.data.items),
  manualImport: (email: string, content: any) =>
    api.post(`/accounts/pending/${encodeURIComponent(email)}/manual-import`, content).then(r => r.data),
  removePending: (email: string) =>
    api.delete(`/accounts/pending/${encodeURIComponent(email)}`).then(() => null),
  syncOne: (email: string, forceRefresh = false) =>
    api.post<{ ok: boolean; msg: string; email: string }>(
      `/accounts/${encodeURIComponent(email)}/sync-cpa`, null,
      { params: { force_refresh: forceRefresh } },
    ).then(r => r.data),
  syncBatch: (batchId: string, forceRefresh = false) =>
    api.post<{
      batch_id: string; total: number; pushed: number; failed: number; skipped: number
      results: { email: string; ok: boolean; msg: string }[]
    }>(`/accounts/batch/${encodeURIComponent(batchId)}/sync-cpa`, null,
      { params: { force_refresh: forceRefresh } },
    ).then(r => r.data),
}
