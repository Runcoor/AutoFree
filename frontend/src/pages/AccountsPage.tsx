import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Search, Download, Copy, Eye, EyeOff, ExternalLink, Check, Clock, AlertCircle, Sparkles, Cloud, RefreshCw, ChevronLeft, ChevronRight, Users, KeyRound, UserPlus, X } from 'lucide-react'
import { accountsApi, freegenApi, type Account, type FreegenStatus } from '../api/endpoints'
import { Button, Card, CardHeader, LiveDot, Pill, ProgressBar, Textarea, useToast } from '../components/ui'

type Filter = 'all' | 'synced' | 'unsynced' | 'failed'

interface CpaStats {
  total: number
  synced: number
  failed: number
  unsynced: number
  sync_rate: number
}

export function AccountsPage() {
  const [items, setItems] = useState<Account[]>([])
  const [page, setPage] = useState(1)
  const [pageSize] = useState(50)
  const [total, setTotal] = useState(0)
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const [show, setShow] = useState<Record<number, boolean>>({})
  const [stats, setStats] = useState<CpaStats | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [reconcileBusy, setReconcileBusy] = useState(false)
  const [reauthing, setReauthing] = useState<string | null>(null)
  const [resumeStatus, setResumeStatus] = useState<FreegenStatus | null>(null)
  const [showAddModal, setShowAddModal] = useState(false)
  const push = useToast((s) => s.push)

  function refreshAll() {
    accountsApi.cpaStats().then(setStats).catch(() => {})
    accountsApi.list({
      page, page_size: pageSize,
      // 后端只支持 cpa_synced bool 筛选;failed 客户端再过滤一道
      cpa_synced: filter === 'all' ? undefined : filter === 'synced' ? true : false,
    }).then((r) => { setItems(r.items); setTotal(r.total) })
  }

  useEffect(() => { refreshAll() }, [page, pageSize, filter])

  // 周期 fetch resume status — reauth 在跑时显示进度卡
  useEffect(() => {
    let cancelled = false
    const tick = () => {
      if (cancelled) return
      freegenApi.status().then((s) => {
        const live = s && Object.keys(s).length > 0 ? s : null
        setResumeStatus(live)
      }).catch(() => {})
    }
    tick()
    const t = setInterval(tick, 3000)
    return () => { cancelled = true; clearInterval(t) }
  }, [])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const searched = search ? items.filter((a) => a.email.includes(search)) : items
  const filtered = filter === 'failed'
    ? searched.filter((a) => !a.cpa_synced && !!a.cpa_error)
    : filter === 'unsynced'
      ? searched.filter((a) => !a.cpa_synced && !a.cpa_error)
      : searched

  async function bulkPushUnsynced() {
    if (!stats || stats.unsynced + stats.failed === 0) return
    if (!confirm(`将自动 refresh + 推送 ${stats.unsynced + stats.failed} 个未同步账号到 CPA(包含之前推送失败的)。\n\n继续?`)) return
    setBulkBusy(true)
    try {
      const r = await accountsApi.syncAllUnsynced()
      push(`完成 — 推 ${r.pushed} · 失败 ${r.failed} · 跳过 ${r.skipped}`,
        r.failed === 0 ? 'success' : r.pushed > 0 ? 'neutral' : 'danger')
      refreshAll()
    } catch (err: any) {
      push(err?.response?.data?.detail || '批量推送失败', 'danger')
    } finally {
      setBulkBusy(false)
    }
  }

  async function reconcile() {
    setReconcileBusy(true)
    try {
      const r = await accountsApi.cpaReconcile()
      const removed = r.removed_on_cpa.length
      const issues = r.status_issues.length
      const tone = removed === 0 && issues === 0 && r.restored === 0 ? 'success' : 'neutral'
      const parts = [
        `CPA 总数 ${r.cpa_total} · 本地 ${r.local_total} · 健康 ${r.healthy}`,
      ]
      if (r.restored > 0) parts.push(`修复 ${r.restored}`)
      if (removed > 0) parts.push(`已被 CPA 删除 ${removed}`)
      if (issues > 0) parts.push(`状态异常 ${issues}`)
      if (r.cpa_only_count > 0) parts.push(`CPA 独有 ${r.cpa_only_count}`)
      push(parts.join(' · '), tone as any)
      refreshAll()
    } catch (err: any) {
      push(err?.response?.data?.detail || '对账失败', 'danger')
    } finally {
      setReconcileBusy(false)
    }
  }

  async function reauth(a: Account) {
    if (resumeStatus && !['finished', 'stopped', 'failed'].includes(resumeStatus.stage || '')) {
      push('已有任务在运行,请等结束', 'danger')
      return
    }
    if (!confirm(`重新认证 ${a.email}?\n\n会用浏览器重新登录该账号 → 跑 OAuth + phone gate(可能消耗 5sim 余额)→ 拿到新 bundle 后写回 + 推 CPA。\n\n适合 refresh_token 已失效的号。`)) return
    setReauthing(a.email)
    try {
      const r = await accountsApi.reauth(a.email)
      push(`已启动重认证 task=${r.task_id}`, 'success')
    } catch (err: any) {
      push(err?.response?.data?.detail || '启动失败', 'danger')
    } finally {
      setReauthing(null)
    }
  }

  const [pushingId, setPushingId] = useState<number | null>(null)
  async function pushOne(a: Account) {
    setPushingId(a.id)
    try {
      const r = await accountsApi.syncOne(a.email)
      push(r.msg, r.ok ? 'success' : 'danger')
      // 局部更新该行
      setItems((curr) => curr.map((x) => x.id === a.id
        ? { ...x, cpa_synced: r.ok && !r.msg.includes('未启用'), cpa_error: r.ok ? null : r.msg }
        : x))
    } catch (err: any) {
      push(err?.response?.data?.detail || '推送失败', 'danger')
    } finally {
      setPushingId(null)
    }
  }

  const cpaPill = (a: Account) => {
    if (a.cpa_synced) return <Pill tone="success"><Check className="w-3 h-3" />已同步</Pill>
    if (a.cpa_error) return <Pill tone="danger"><AlertCircle className="w-3 h-3" />失败</Pill>
    return <Pill tone="warn"><Clock className="w-3 h-3" />未同步</Pill>
  }

  const planPill = (plan: string) => {
    const isPro = plan?.toLowerCase() === 'pro'
    return isPro ? (
      <Pill tone="info"><Sparkles className="w-3 h-3" />{plan}</Pill>
    ) : (
      <Pill tone="muted">{plan || 'Free'}</Pill>
    )
  }

  return (
    <div className="page">
      <div className="flex flex-wrap items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-[32px] font-extrabold tracking-[-0.02em] leading-[1.1] m-0">账号</h1>
          <p className="text-ink-soft text-[14px] mt-1.5">已注册成功的账号 · 共 {total} 个</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <Button onClick={refreshAll}>
            <RefreshCw className="w-3.5 h-3.5" />
            刷新
          </Button>
          <Button
            onClick={() => setShowAddModal(true)}
            title="手动添加已有的 email + password — AutoFree 自动登录拿 token + 推 CPA"
          >
            <UserPlus className="w-3.5 h-3.5" />
            添加账号
          </Button>
          <Button
            onClick={reconcile}
            loading={reconcileBusy}
            title="拉 CPA 现有列表 → 对照本地状态。CPA 上已删的号会被标失败,可重推。"
          >
            <Cloud className="w-3.5 h-3.5" />
            对账 CPA
          </Button>
          <Button
            variant="primary"
            onClick={bulkPushUnsynced}
            loading={bulkBusy}
            disabled={!stats || (stats.unsynced + stats.failed === 0)}
            title={
              stats && stats.unsynced + stats.failed === 0
                ? '所有账号都已同步'
                : '自动 refresh access_token 后,把所有未同步 / 推送失败的号都推到 CPA'
            }
          >
            <Cloud className="w-3.5 h-3.5" />
            推送所有未同步 {stats ? `(${stats.unsynced + stats.failed})` : ''}
          </Button>
        </div>
      </div>

      {/* CPA 同步概览 */}
      {stats && (
        <div className="grid gap-3 grid-cols-2 sm:grid-cols-4 mb-5 anim-in">
          <CpaStatCard
            label="账号总数"
            value={stats.total}
            icon={<Users className="w-4 h-4" />}
            tone="muted"
            active={filter === 'all'}
            onClick={() => { setFilter('all'); setPage(1) }}
          />
          <CpaStatCard
            label="已推 CPA"
            value={stats.synced}
            icon={<Check className="w-4 h-4" />}
            tone="success"
            sub={stats.total ? `${Math.round(stats.sync_rate * 100)}%` : ''}
            active={filter === 'synced'}
            onClick={() => { setFilter('synced'); setPage(1) }}
          />
          <CpaStatCard
            label="未推 CPA"
            value={stats.unsynced}
            icon={<Clock className="w-4 h-4" />}
            tone="warn"
            active={filter === 'unsynced'}
            onClick={() => { setFilter('unsynced'); setPage(1) }}
          />
          <CpaStatCard
            label="推送失败"
            value={stats.failed}
            icon={<AlertCircle className="w-4 h-4" />}
            tone="danger"
            active={filter === 'failed'}
            onClick={() => { setFilter('failed'); setPage(1) }}
          />
        </div>
      )}

      {/* Reauth in progress */}
      {resumeStatus && resumeStatus.task_id && !['finished', 'stopped', 'failed'].includes(resumeStatus.stage || '') && (
        <Card className="mb-5 anim-in">
          <CardHeader
            title={
              <span className="flex items-center gap-2">
                {resumeStatus.task_id?.startsWith('reauth') ? '重认证中' : '任务进行中'}
                <span className="mono text-[13px] text-ink-soft">{resumeStatus.current_email}</span>
              </span>
            }
            subtitle={`stage=${resumeStatus.stage}`}
            action={
              <Pill tone="info">
                <LiveDot tone="info" />
                运行中
              </Pill>
            }
          />
          <div className="px-6 pb-5">
            <ProgressBar value={(resumeStatus.ok || 0) + (resumeStatus.failed || 0)} total={resumeStatus.total || 1} />
          </div>
        </Card>
      )}

      <Card className="anim-in">
        <CardHeader
          title={
            <span className="flex items-center gap-2.5">
              账号列表
              <Pill tone="muted">{filtered.length} 个</Pill>
            </span>
          }
          action={
            <div className="flex items-center gap-2.5 flex-wrap">
              <div className="flex items-center gap-2 px-3 h-9 bg-bg-soft border border-line rounded-[10px]">
                <Search className="w-3.5 h-3.5 text-ink-soft" />
                <input
                  className="bg-transparent border-none outline-none text-[13px] w-[180px] text-ink placeholder:text-ink-faint"
                  placeholder="搜索邮箱…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              <div className="seg">
                {(['all', 'synced', 'unsynced', 'failed'] as Filter[]).map((v) => (
                  <button
                    key={v}
                    type="button"
                    className={filter === v ? 'active' : ''}
                    onClick={() => { setFilter(v); setPage(1) }}
                  >
                    {v === 'all' ? '全部' : v === 'synced' ? '已同步' : v === 'unsynced' ? '未同步' : '失败'}
                  </button>
                ))}
              </div>
            </div>
          }
        />
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>邮箱</th>
                <th>密码</th>
                <th>Plan</th>
                <th>CPA</th>
                <th>创建时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={6}>
                    <div className="empty-state">
                      <div className="empty-icon"><Search size={22} /></div>
                      暂无匹配的账号
                    </div>
                  </td>
                </tr>
              )}
              {filtered.map((a) => (
                <tr key={a.id}>
                  <td>
                    <div className="flex items-center gap-2.5">
                      <div className="w-8 h-8 rounded-[8px] grad-bg text-white grid place-items-center font-bold text-[13px] shrink-0">
                        {(a.email[0] || '?').toUpperCase()}
                      </div>
                      <span className="mono text-[13px] truncate max-w-[240px]" title={a.email}>{a.email}</span>
                    </div>
                  </td>
                  <td>
                    <div className="flex items-center gap-2">
                      <span className="mono text-[13px] tracking-[0.5px]">
                        {show[a.id] ? a.password : '••••••••••'}
                      </span>
                      <button
                        type="button"
                        className="btn btn-ghost btn-icon !w-6 !h-6"
                        onClick={() => setShow((s) => ({ ...s, [a.id]: !s[a.id] }))}
                        aria-label="显示密码"
                      >
                        {show[a.id] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-icon !w-6 !h-6"
                        onClick={() => {
                          navigator.clipboard?.writeText(`${a.email}:${a.password}`)
                          push('已复制 邮箱:密码', 'success')
                        }}
                        aria-label="复制"
                      >
                        <Copy className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </td>
                  <td>{planPill(a.plan_type)}</td>
                  <td>{cpaPill(a)}</td>
                  <td className="text-ink-soft">
                    {a.created_at ? new Date(a.created_at).toLocaleString('zh-CN') : '—'}
                  </td>
                  <td>
                    <div className="flex items-center gap-1 whitespace-nowrap">
                      <button
                        type="button"
                        title="自动 refresh + 推 CPA(refresh_token 还有效时用这个)"
                        className="btn btn-ghost btn-icon"
                        disabled={pushingId === a.id}
                        onClick={() => pushOne(a)}
                      >
                        {pushingId === a.id
                          ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                          : <Cloud className="w-3.5 h-3.5" />}
                      </button>
                      <button
                        type="button"
                        title="重新登录 → 重跑 OAuth → 推 CPA(refresh_token 失效时用这个)"
                        className="btn btn-ghost btn-icon"
                        disabled={reauthing === a.email || !a.password}
                        onClick={() => reauth(a)}
                      >
                        {reauthing === a.email
                          ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                          : <KeyRound className="w-3.5 h-3.5" />}
                      </button>
                      <a
                        href={accountsApi.download(a.email)}
                        title="下载 auth.json"
                        className="btn btn-ghost btn-icon"
                      >
                        <Download className="w-3.5 h-3.5" />
                      </a>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex items-center justify-between px-6 py-3.5 border-t border-line">
          <span className="text-[13px] text-ink-soft">
            第 {page} / {totalPages} 页 · 共 {total} 个
          </span>
          <div className="flex items-center gap-2">
            <Button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              <ChevronLeft className="w-3.5 h-3.5" />
              上一页
            </Button>
            <Button disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
              下一页
              <ChevronRight className="w-3.5 h-3.5" />
            </Button>
          </div>
        </div>
      </Card>

      {showAddModal && (
        <ManualAddModal
          onClose={() => setShowAddModal(false)}
          onSubmitted={() => { setShowAddModal(false); refreshAll() }}
        />
      )}
    </div>
  )
}

function ManualAddModal({
  onClose, onSubmitted,
}: { onClose: () => void; onSubmitted: () => void }) {
  const [mode, setMode] = useState<'single' | 'bulk'>('single')
  const [email, setEmail] = useState('')
  const [bulk, setBulk] = useState('')
  const [busy, setBusy] = useState(false)
  const push = useToast((s) => s.push)

  // 批量:每行一个 email
  function parseBulkEmails(text: string): string[] {
    return text.split(/\r?\n/)
      .map((l) => l.trim().toLowerCase())
      .filter((l) => l && l.includes('@'))
  }

  const previewBulk = mode === 'bulk' ? parseBulkEmails(bulk) : []

  async function submit() {
    let emails: string[]
    if (mode === 'single') {
      const e = email.trim().toLowerCase()
      if (!e || !e.includes('@')) return push('email 格式不对', 'danger')
      emails = [e]
    } else {
      emails = previewBulk
      if (emails.length === 0) return push('没有可解析的 email(每行一个)', 'danger')
    }
    if (!confirm(
      `提交 ${emails.length} 个 email?\n\n` +
      `系统会自动从 cloud-mail 取登录 OTP → 走 chatgpt.com 邮箱验证码登录 → 拿 codex token → 推 CPA。\n` +
      `每个号都会触发一次 OTP 邮件,可能还有 phone gate(消耗 5sim 余额)。\n\n` +
      `要求:邮箱域名必须已配在 cloud-mail。\n` +
      `成功 → 进「账号」页;失败 → 进「待办」页可继续验证。`,
    )) return
    setBusy(true)
    try {
      const accounts = emails.map((e) => ({ email: e }))
      const r = await freegenApi.manualAdd(accounts)
      const skipMsg = r.skipped_existing.length
        ? ` · 跳过已存在 ${r.skipped_existing.length}`
        : ''
      const dupMsg = r.skipped_duplicate.length ? ` · 去重 ${r.skipped_duplicate.length}` : ''
      push(`已启动 · ${r.total} 个号串行跑${skipMsg}${dupMsg} — 进度看「注册批次」页`, 'success')
      onSubmitted()
    } catch (err: any) {
      push(err?.response?.data?.detail || '启动失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  // Portal 到 body — 绕开 .page 的 transform 容器,确保遮罩盖满 viewport
  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 640 }}>
        <div className="card-header">
          <div>
            <h3 className="flex items-center gap-2">
              <UserPlus className="w-4 h-4" />
              手动添加账号
            </h3>
            <div className="sub">填 email,系统自动从 cloud-mail 取 OTP 登录 + 推 CPA</div>
          </div>
          <button onClick={onClose} className="btn btn-ghost btn-icon" aria-label="关闭">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="card-body">
          {/* 模式切换 */}
          <div className="flex gap-1 p-1 bg-bg-soft rounded-[10px] border border-line w-fit mb-4">
            <button
              type="button"
              className={
                'px-3 py-1.5 rounded-[8px] text-[12.5px] font-medium transition ' +
                (mode === 'single' ? 'grad-bg text-white shadow-glow' : 'text-ink-soft hover:text-ink')
              }
              onClick={() => setMode('single')}
            >
              单条
            </button>
            <button
              type="button"
              className={
                'px-3 py-1.5 rounded-[8px] text-[12.5px] font-medium transition ' +
                (mode === 'bulk' ? 'grad-bg text-white shadow-glow' : 'text-ink-soft hover:text-ink')
              }
              onClick={() => setMode('bulk')}
            >
              批量(每行一个 email)
            </button>
          </div>

          {mode === 'single' ? (
            <div className="field">
              <label>邮箱</label>
              <input
                className="input mono"
                placeholder="user@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoFocus
              />
            </div>
          ) : (
            <div className="space-y-2">
              <Textarea
                rows={8}
                value={bulk}
                onChange={(e) => setBulk(e.target.value)}
                placeholder={'每行一个 email:\na@example.com\nb@example.com\nc@example.com'}
              />
              <div className="flex items-center justify-between text-[12px]">
                <span className="text-ink-soft">
                  解析到 <span className="grad-text font-bold">{previewBulk.length}</span> 个 email
                </span>
                {previewBulk.length > 0 && (
                  <span className="text-ink-faint mono truncate max-w-[360px]">
                    示例:{previewBulk.slice(0, 2).join(', ')}
                    {previewBulk.length > 2 && ` …+${previewBulk.length - 2}`}
                  </span>
                )}
              </div>
            </div>
          )}

          <div className="text-[11.5px] text-ink-faint mt-4 leading-relaxed border-t border-line pt-3">
            <div className="flex items-start gap-1.5">
              <AlertCircle className="w-3 h-3 shrink-0 mt-0.5 text-warn" />
              <div>
                邮箱域名必须已配在 <strong>cloud-mail</strong>(才能收 OTP)。
                登录走「邮箱验证码」方式 — 不需要密码。
                若 OpenAI 要 phone gate,会自动用 5sim 拿号(消耗余额)。
                失败的会进「待办」页可继续重试。
              </div>
            </div>
          </div>

          <div className="flex justify-end gap-2 mt-4">
            <Button onClick={onClose}>取消</Button>
            <Button variant="primary" onClick={submit} loading={busy}
              disabled={mode === 'single' ? !email : previewBulk.length === 0}>
              <Check className="w-3.5 h-3.5" />
              {mode === 'single' ? '添加' : `添加 ${previewBulk.length} 个`}
            </Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function CpaStatCard({
  label, value, sub, icon, tone, active, onClick,
}: {
  label: string
  value: number
  sub?: string
  icon: React.ReactNode
  tone: 'muted' | 'success' | 'warn' | 'danger'
  active?: boolean
  onClick?: () => void
}) {
  const accent = {
    muted: 'text-ink-soft bg-bg-soft',
    success: 'text-success bg-success/10',
    warn: 'text-warn bg-warn/10',
    danger: 'text-danger bg-danger/10',
  }[tone]
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        'card text-left transition-all duration-150 ' +
        (active ? '!border-brand-1 shadow-glow' : 'hover:-translate-y-0.5 hover:shadow-md')
      }
    >
      <div className="px-5 py-4">
        <div className="flex items-center justify-between mb-2.5">
          <div className={`w-9 h-9 rounded-[10px] grid place-items-center ${accent}`}>
            {icon}
          </div>
          {sub && <span className="text-[11px] font-semibold text-ink-soft">{sub}</span>}
        </div>
        <div className="text-[28px] font-extrabold tracking-tight tabular-nums">
          {value}
        </div>
        <div className="text-[12.5px] text-ink-soft mt-0.5">{label}</div>
      </div>
    </button>
  )
}
