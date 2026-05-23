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
export interface SmsProviderBlock {
  api_key_masked: string; has_api_key: boolean
  service: string; country: string; operator: string
  max_price?: string  // 最大单价 USD,留空=不限
}
export interface SmsCfg {
  active: string  // '5sim' | 'hero-sms'
  providers: Record<string, SmsProviderBlock>
  // 兼容字段(active provider 的扁平视图)
  provider: string; api_key_masked: string; has_api_key: boolean
  service: string; country: string; operator: string
  msg?: string
}
export interface CpaCfg { url: string; key_masked: string; has_key: boolean; enabled: boolean; msg?: string }

export interface ProxyCfg {
  enabled: boolean
  provider: string  // 'iproyal-residential' | 'iproyal-mobile' | 'custom'
  providers_known: string[]
  provider_defaults: Record<string, { host: string; port: string; country: string; lifetime: string }>
  host: string
  port: string
  username: string
  password_masked: string
  has_password: boolean
  country: string
  lifetime: string
  msg?: string
}

export interface ProxyTestResult {
  ok: boolean
  ip: string
  country: string
  region: string
  city: string
  org: string
  timezone: string
  session_user: string
  raw: any
}

export const settingsApi = {
  getCloudMail: () => api.get<CloudMailCfg>('/settings/cloud-mail').then(r => r.data),
  putCloudMail: (body: Partial<{ base_url: string; password: string }>) =>
    api.put<CloudMailCfg>('/settings/cloud-mail', body).then(r => r.data),
  getSms: () => api.get<SmsCfg>('/settings/sms').then(r => r.data),
  putSms: (body: {
    provider: string  // '5sim' | 'hero-sms' — 必传,决定写入哪个 namespace
    api_key?: string; service?: string; country?: string; operator?: string
    max_price?: string  // 最大单价 USD,留空=不限
    set_active?: boolean
  }) => api.put<SmsCfg>('/settings/sms', body).then(r => r.data),
  setSmsActive: (provider: string) =>
    api.post<SmsCfg>('/settings/sms/active', { provider }).then(r => r.data),
  smsBalance: (provider?: string) =>
    api.post<{ provider: string; balance: number; currency: string; raw?: any }>(
      '/settings/sms/balance', null, { params: provider ? { provider } : undefined },
    ).then(r => r.data),
  getCpa: () => api.get<CpaCfg>('/settings/cpa').then(r => r.data),
  putCpa: (body: Partial<{ url: string; key: string; enabled: boolean }>) =>
    api.put<CpaCfg>('/settings/cpa', body).then(r => r.data),
  getProxy: () => api.get<ProxyCfg>('/settings/proxy').then(r => r.data),
  putProxy: (body: Partial<{
    enabled: boolean; provider: string; host: string; port: string
    username: string; password: string; country: string; lifetime: string
  }>) => api.put<ProxyCfg>('/settings/proxy', body).then(r => r.data),
  proxyTest: () => api.post<ProxyTestResult>('/settings/proxy/test').then(r => r.data),
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
  reg_mode?: 'email' | 'phone'
}
export interface Batch {
  id: string; domain: string; count: number; status: string
  started_at: string | null; finished_at: string | null
  ok: number; failed: number; created_at: string | null
}

export interface BatchDetailResult {
  index: number
  ok: boolean
  email?: string
  password?: string
  error?: string
  error_kind?: string
  account_id?: string
  plan_type?: string
  cpa_pushed?: boolean
  cpa_msg?: string
  auth_file?: string
  register_secs?: number
  oauth_secs?: number
}
export interface BatchDetail {
  batch: Batch
  accounts: Account[]
  pending: PendingAccount[]
  results: BatchDetailResult[]
  summary: {
    total: number
    ok: number
    failed: number
    cpa_pushed: number
    cpa_unpushed: number
    pending: number
  }
}

export const freegenApi = {
  start: (count: number, domain?: string, domain_mode: 'fixed' | 'rotate' | 'random' = 'rotate',
         reg_mode: 'email' | 'phone' = 'email') =>
    api.post<{
      task_id: string; batch_id: string; domain: string
      domain_mode: 'fixed' | 'rotate' | 'random'
      reg_mode: 'email' | 'phone'
      random_pool: string[]
      count: number
    }>('/freegen/start', { count, domain, domain_mode, reg_mode }).then(r => r.data),
  stop: () => api.post<{ ok: true; msg: string }>('/freegen/stop').then(r => r.data),
  status: () => api.get<FreegenStatus>('/freegen/status').then(r => r.data),
  batches: (limit = 20) => api.get<{ items: Batch[] }>('/freegen/batches', { params: { limit } }).then(r => r.data.items),
  batchDetail: (batch_id: string) =>
    api.get<BatchDetail>(`/freegen/batches/${encodeURIComponent(batch_id)}`).then(r => r.data),
  deleteBatch: (batch_id: string, drop_dir = true) =>
    api.delete(`/freegen/batches/${encodeURIComponent(batch_id)}`, { params: { drop_dir } }).then(() => null),
  resume: (email: string) =>
    api.post<{ task_id: string; batch_id: string; email: string; mode: 'resume' }>('/freegen/resume',
      { email }).then(r => r.data),
  resumeAll: (emails?: string[]) =>
    api.post<{ task_id: string; total: number; skipped_no_password: number; mode: 'resume_all' }>(
      '/freegen/resume-all',
      emails && emails.length > 0 ? { emails } : {},
    ).then(r => r.data),
  manualAdd: (accounts: { email: string; password?: string }[]) =>
    api.post<{
      task_id: string; batch_id: string; total: number
      skipped_existing: string[]
      skipped_duplicate: string[]
      mode: 'manual_add'
    }>('/freegen/manual-add', { accounts }).then(r => r.data),
}

// ─── accounts ───────────────────────────────────────────────
export interface CpaInventoryItem {
  name: string
  id: string
  email: string
  type: string
  status: string
  status_message: string
  disabled: boolean
  unavailable: boolean
  size: number | null
  updated_at: string | null
  success: number | null
  failed: number | null
  in_local: boolean
  local_cpa_error: string
  is_dead: boolean
  is_failed_state: boolean
}

export interface Account {
  id: number; batch_id: string; email: string; password: string
  account_id: string; plan_type: string
  expires_at: string | null
  auth_json_path: string
  cpa_synced: boolean; cpa_synced_at: string | null; cpa_error: string | null
  phone_verified: boolean; phone_verified_at: string | null
  created_at: string | null
}
export interface PendingAccount {
  id: number; batch_id: string; email: string; password: string
  error_kind: string; error: string
  phone_verified: boolean; phone_verified_at: string | null
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
  cpaStats: () =>
    api.get<{ total: number; synced: number; failed: number; unsynced: number; sync_rate: number }>(
      '/accounts/cpa-stats',
    ).then(r => r.data),
  cpaReconcile: () =>
    api.post<{
      cpa_total: number; local_total: number; cpa_only_count: number
      healthy: number; restored: number
      removed_on_cpa: string[]
      status_issues: { email: string; status: string }[]
    }>('/accounts/cpa-reconcile').then(r => r.data),
  cpaInventory: () =>
    api.get<{
      items: CpaInventoryItem[]
      summary: {
        total: number; active: number; disabled: number; unavailable: number
        other_status: number; in_local: number; cpa_only: number
      }
    }>('/accounts/cpa-inventory').then(r => r.data),
  cpaDelete: (names: string[]) =>
    api.post<{
      total: number; succeeded: number; failed: number
      affected_local_count: number
      affected_local_emails: string[]
      results: { name: string; ok: boolean; msg: string }[]
    }>('/accounts/cpa-inventory/delete', { names }).then(r => r.data),
  screenshots: () =>
    api.get<{
      items: { name: string; size: number; mtime: number; mtime_iso: string }[]
      total: number
    }>('/screenshots').then(r => r.data),
  screenshotUrl: (name: string) =>
    `/api/screenshots/file?name=${encodeURIComponent(name)}`,
  cpaReauth: (params: { emails?: string[]; names?: string[] }) =>
    api.post<{
      task_id: string
      total: number
      emails?: string[]
      skipped: { email?: string; name?: string; reason: string }[]
      mode: string
      msg?: string
    }>('/accounts/cpa-inventory/reauth', params).then(r => r.data),
  syncAllUnsynced: (forceRefresh = false, includeFailed = true) =>
    api.post<{
      total: number; pushed: number; failed: number; skipped: number
      results: { email: string; ok: boolean; msg: string }[]
    }>('/accounts/sync-cpa/all-unsynced', null,
      { params: { force_refresh: forceRefresh, include_failed: includeFailed } },
    ).then(r => r.data),
  reauth: (email: string) =>
    api.post<{ task_id: string; batch_id: string; email: string; mode: 'reauth' }>(
      `/accounts/${encodeURIComponent(email)}/re-auth`,
    ).then(r => r.data),
}
