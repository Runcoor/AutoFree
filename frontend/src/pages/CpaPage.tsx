import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Cloud, RefreshCw, Trash2, Search, Check, AlertCircle, X, Filter as FilterIcon,
  Database, ShieldOff, Power, KeyRound,
} from 'lucide-react'
import { accountsApi, type CpaInventoryItem } from '../api/endpoints'
import { Button, Card, CardHeader, Pill, useToast } from '../components/ui'

type Tab = 'all' | 'failed' | 'cpa_only' | 'in_local'

export function CpaPage() {
  const [items, setItems] = useState<CpaInventoryItem[]>([])
  const [summary, setSummary] = useState<{
    total: number; active: number; disabled: number; unavailable: number
    other_status: number; in_local: number; cpa_only: number
  } | null>(null)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState<Tab>('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [deleting, setDeleting] = useState<string | null>(null)
  const [reauthing, setReauthing] = useState<string | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const push = useToast((s) => s.push)
  const navigate = useNavigate()

  useEffect(() => { refresh() }, [])

  async function refresh() {
    setLoading(true)
    try {
      const r = await accountsApi.cpaInventory()
      setItems(r.items)
      setSummary(r.summary)
      setSelected(new Set())
    } catch (err: any) {
      push(err?.response?.data?.detail || '拉 CPA 列表失败', 'danger')
    } finally {
      setLoading(false)
    }
  }

  const filtered = useMemo(() => {
    let xs = items
    if (tab === 'failed') xs = xs.filter((x) => x.is_failed_state)
    else if (tab === 'cpa_only') xs = xs.filter((x) => !x.in_local)
    else if (tab === 'in_local') xs = xs.filter((x) => x.in_local)
    if (search.trim()) {
      const q = search.trim().toLowerCase()
      xs = xs.filter((x) =>
        (x.email || '').toLowerCase().includes(q) ||
        (x.name || '').toLowerCase().includes(q),
      )
    }
    return xs
  }, [items, tab, search])

  const allFilteredSelected = filtered.length > 0
    && filtered.every((x) => selected.has(x.name))
  const someFilteredSelected = filtered.some((x) => selected.has(x.name))

  function toggleAll() {
    setSelected((prev) => {
      if (allFilteredSelected) {
        const next = new Set(prev)
        for (const x of filtered) next.delete(x.name)
        return next
      }
      const next = new Set(prev)
      for (const x of filtered) next.add(x.name)
      return next
    })
  }

  function toggleOne(name: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  async function deleteOne(item: CpaInventoryItem) {
    const label = item.email || item.name
    if (!confirm(`从 CPA 删除 ${label}?(本地 DB 保留)`)) return
    setDeleting(item.name)
    try {
      const r = await accountsApi.cpaDelete([item.name])
      const result = r.results[0]
      if (result?.ok) {
        push(`已从 CPA 删除 ${label}`, 'success')
        await refresh()
      } else {
        push(`删除失败:${result?.msg || '未知错误'}`, 'danger')
      }
    } catch (err: any) {
      push(err?.response?.data?.detail || '删除失败', 'danger')
    } finally {
      setDeleting(null)
    }
  }

  async function deleteSelected() {
    if (selected.size === 0) return
    if (!confirm(`从 CPA 删除选中 ${selected.size} 个?`)) return
    setBulkBusy(true)
    try {
      const r = await accountsApi.cpaDelete(Array.from(selected))
      const tone = r.failed === 0 ? 'success' : r.succeeded > 0 ? 'neutral' : 'danger'
      push(`完成 — 成功 ${r.succeeded} · 失败 ${r.failed}${r.affected_local_count ? ` · 同步本地 ${r.affected_local_count}` : ''}`, tone as any)
      await refresh()
    } catch (err: any) {
      push(err?.response?.data?.detail || '批量删除失败', 'danger')
    } finally {
      setBulkBusy(false)
    }
  }

  async function deleteAllDead() {
    const dead = items.filter((x) => x.is_dead)
    if (dead.length === 0) {
      push('当前没有已废号', 'neutral')
      return
    }
    if (!confirm(`删除 ${dead.length} 个已废号(account_deactivated)?`)) return
    setBulkBusy(true)
    try {
      const r = await accountsApi.cpaDelete(dead.map((x) => x.name))
      const tone = r.failed === 0 ? 'success' : r.succeeded > 0 ? 'neutral' : 'danger'
      push(`完成 — 成功 ${r.succeeded} · 失败 ${r.failed}`, tone as any)
      await refresh()
    } catch (err: any) {
      push(err?.response?.data?.detail || '删除失败', 'danger')
    } finally {
      setBulkBusy(false)
    }
  }

  async function deleteAllFailed() {
    const failed = items.filter((x) => x.is_failed_state)
    if (failed.length === 0) {
      push('当前没有失败状态的号', 'neutral')
      return
    }
    if (!confirm(`清理 ${failed.length} 个失败状态号?`)) return
    setBulkBusy(true)
    try {
      const r = await accountsApi.cpaDelete(failed.map((x) => x.name))
      const tone = r.failed === 0 ? 'success' : r.succeeded > 0 ? 'neutral' : 'danger'
      push(`清理完成 — 成功 ${r.succeeded} · 失败 ${r.failed}`, tone as any)
      await refresh()
    } catch (err: any) {
      push(err?.response?.data?.detail || '一键清理失败', 'danger')
    } finally {
      setBulkBusy(false)
    }
  }

  async function reauthOne(item: CpaInventoryItem) {
    if (!item.email) {
      push('该项缺 email,无法 reauth', 'danger')
      return
    }
    const label = item.email
    if (!confirm(`重新认证 ${label}?`)) return
    setReauthing(item.name)
    try {
      const r = await accountsApi.cpaReauth({ emails: [item.email] })
      if (r.total === 0) {
        push(r.msg || '没有可执行账号', 'danger')
        return
      }
      push(`已启动重新认证(task=${r.task_id})— 跳转到批次实时进度`, 'success')
      navigate('/batch')
    } catch (err: any) {
      push(err?.response?.data?.detail || '启动失败', 'danger')
    } finally {
      setReauthing(null)
    }
  }

  async function reauthSelected() {
    const selectedItems = items.filter((x) => selected.has(x.name))
    const reAuthable = selectedItems.filter((x) => x.email)
    const skipped = selectedItems.length - reAuthable.length
    if (reAuthable.length === 0) {
      push('选中项缺 email,无法 reauth', 'danger')
      return
    }
    if (!confirm(
      `批量重新认证 ${reAuthable.length} 个?` +
      (skipped > 0 ? `(跳过 ${skipped} 个无 email)` : ''),
    )) return
    setBulkBusy(true)
    try {
      const r = await accountsApi.cpaReauth({ emails: reAuthable.map((x) => x.email) })
      push(`已启动批量重新认证(${r.total} 个)— task=${r.task_id}`, 'success')
      navigate('/batch')
    } catch (err: any) {
      push(err?.response?.data?.detail || '启动失败', 'danger')
    } finally {
      setBulkBusy(false)
    }
  }

  async function reauthAllFailedLocal() {
    const targets = items.filter((x) => x.is_failed_state && x.email && !x.is_dead)
    if (targets.length === 0) {
      push('没有可重认证的失败号(已废号请直接删除)', 'neutral')
      return
    }
    if (!confirm(`重新认证全部失败号 ${targets.length} 个?(已自动跳过号已废)`)) return
    setBulkBusy(true)
    try {
      const r = await accountsApi.cpaReauth({ emails: targets.map((x) => x.email) })
      push(`已启动(${r.total} 个)— task=${r.task_id}`, 'success')
      navigate('/batch')
    } catch (err: any) {
      push(err?.response?.data?.detail || '启动失败', 'danger')
    } finally {
      setBulkBusy(false)
    }
  }

  const failedCount = items.filter((x) => x.is_failed_state).length
  const deadCount = items.filter((x) => x.is_dead).length
  const failedLocalCount = items.filter((x) => x.is_failed_state && x.email && !x.is_dead).length
  const selectedReauthableCount = items.filter((x) => selected.has(x.name) && x.email && !x.is_dead).length

  return (
    <div className="page">
      <div className="flex flex-wrap items-start justify-between gap-4 mb-7">
        <div>
          <h1 className="text-[32px] font-extrabold tracking-[-0.02em] leading-[1.1] m-0">CPA 全景</h1>
          <p className="text-ink-soft text-[14px] mt-1.5">
            管理 CLIProxyAPI 上的所有 auth-files · 跨工具来源(包含非 AutoFree 注册的)
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {deadCount > 0 && (
            <Button variant="danger" onClick={deleteAllDead} loading={bulkBusy}>
              <span style={{ fontSize: 14, lineHeight: 1 }}>🪦</span>
              删除已废号 ({deadCount})
            </Button>
          )}
          {failedLocalCount > 0 && (
            <Button variant="primary" onClick={reauthAllFailedLocal} loading={bulkBusy}>
              <KeyRound className="w-3.5 h-3.5" />
              重新认证失败号 ({failedLocalCount})
            </Button>
          )}
          {failedCount > 0 && (
            <Button variant="danger" onClick={deleteAllFailed} loading={bulkBusy}>
              <ShieldOff className="w-3.5 h-3.5" />
              清理失败状态 ({failedCount})
            </Button>
          )}
          <Button onClick={refresh} loading={loading}>
            <RefreshCw className="w-3.5 h-3.5" />
            刷新
          </Button>
        </div>
      </div>

      {/* Stat cards */}
      {summary && (
        <div className="grid gap-3 grid-cols-2 md:grid-cols-4 mb-5">
          <StatCard
            label="CPA 总数" value={summary.total}
            icon={<Cloud size={18} />} tone="info"
            active={tab === 'all'} onClick={() => setTab('all')}
          />
          <StatCard
            label="本地有的" value={summary.in_local}
            icon={<Database size={18} />} tone="success"
            active={tab === 'in_local'} onClick={() => setTab('in_local')}
          />
          <StatCard
            label="仅 CPA 有" value={summary.cpa_only}
            icon={<Cloud size={18} />} tone="muted"
            active={tab === 'cpa_only'} onClick={() => setTab('cpa_only')}
          />
          <StatCard
            label="失败状态" value={summary.disabled + summary.unavailable + summary.other_status}
            icon={<ShieldOff size={18} />} tone="danger"
            active={tab === 'failed'} onClick={() => setTab('failed')}
          />
        </div>
      )}

      <Card className="anim-in mb-5">
        <CardHeader
          title={
            <span className="flex items-center gap-2">
              CPA Auth-Files
              <Pill tone="muted">{filtered.length} / {items.length}</Pill>
            </span>
          }
          subtitle={
            tab === 'all' ? '全部 auth-files'
              : tab === 'in_local' ? '本地 AutoFree DB 也有的'
                : tab === 'cpa_only' ? '只在 CPA 上(非本工具注册)'
                  : '失败状态(disabled / unavailable / 非 active)'
          }
          action={
            <div className="flex items-center gap-2">
              <div className="relative">
                <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint pointer-events-none" />
                <input
                  className="input !pl-8 !h-[34px] !text-[13px] w-[220px]"
                  placeholder="搜索 email / 文件名…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                {search && (
                  <button
                    type="button"
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-faint hover:text-ink"
                    onClick={() => setSearch('')}
                    aria-label="清空"
                  >
                    <X className="w-3 h-3" />
                  </button>
                )}
              </div>
              {selected.size > 0 && selectedReauthableCount > 0 && (
                <Button variant="primary" onClick={reauthSelected} loading={bulkBusy}>
                  <KeyRound className="w-3.5 h-3.5" />
                  重新认证 ({selectedReauthableCount})
                </Button>
              )}
              {selected.size > 0 && (
                <Button variant="danger" onClick={deleteSelected} loading={bulkBusy}>
                  <Trash2 className="w-3.5 h-3.5" />
                  删除选中 ({selected.size})
                </Button>
              )}
            </div>
          }
        />
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 36 }}>
                  <input
                    type="checkbox"
                    checked={allFilteredSelected}
                    ref={(el) => { if (el) el.indeterminate = !allFilteredSelected && someFilteredSelected }}
                    onChange={toggleAll}
                    aria-label="全选"
                  />
                </th>
                <th>邮箱</th>
                <th>来源</th>
                <th>状态</th>
                <th>大小</th>
                <th>更新时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={7}>
                    <div className="empty-state">
                      <div className="empty-icon"><Cloud size={22} /></div>
                      {loading ? '加载中…' : items.length === 0 ? 'CPA 上没有 auth-file' : '当前筛选条件下无结果'}
                    </div>
                  </td>
                </tr>
              )}
              {filtered.map((it) => (
                <tr key={it.name} className={selected.has(it.name) ? 'bg-bg-soft' : ''}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(it.name)}
                      onChange={() => toggleOne(it.name)}
                      aria-label={`选 ${it.email || it.name}`}
                    />
                  </td>
                  <td>
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div
                        className="w-8 h-8 rounded-[8px] grid place-items-center shrink-0"
                        style={{
                          background: it.is_failed_state
                            ? 'rgba(239,68,68,0.15)'
                            : it.in_local
                              ? 'rgba(16,185,129,0.15)'
                              : 'rgba(100,116,139,0.15)',
                          color: it.is_failed_state
                            ? 'var(--danger)'
                            : it.in_local
                              ? 'var(--success)'
                              : 'var(--ink-soft)',
                        }}
                      >
                        {it.is_failed_state ? <AlertCircle className="w-3.5 h-3.5" />
                          : it.in_local ? <Database className="w-3.5 h-3.5" />
                            : <Cloud className="w-3.5 h-3.5" />}
                      </div>
                      <div className="min-w-0">
                        <div className="mono text-[13px] truncate max-w-[280px]" title={it.email}>
                          {it.email || <span className="text-ink-faint">(无 email)</span>}
                        </div>
                        <div className="text-[11px] text-ink-faint mono truncate max-w-[280px]" title={it.name}>
                          {it.name}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td>
                    {it.in_local
                      ? <Pill tone="success"><Database className="w-3 h-3" />本地 + CPA</Pill>
                      : <Pill tone="muted"><Cloud className="w-3 h-3" />仅 CPA</Pill>}
                    {it.type && (
                      <div className="mt-1 text-[11px] text-ink-faint mono">{it.type}</div>
                    )}
                  </td>
                  <td>
                    {it.is_dead
                      ? <span title={it.local_cpa_error}><Pill tone="danger">🪦 号已废</Pill></span>
                      : it.disabled
                        ? <Pill tone="danger"><Power className="w-3 h-3" />disabled</Pill>
                        : it.unavailable
                          ? <Pill tone="danger"><AlertCircle className="w-3 h-3" />unavailable</Pill>
                          : it.status === 'active'
                            ? <Pill tone="success"><Check className="w-3 h-3" />active</Pill>
                            : <Pill tone="warn">{it.status || 'unknown'}</Pill>}
                    {(it.is_dead ? it.local_cpa_error : it.status_message) && (
                      <div className="text-[11px] text-ink-faint mt-1 max-w-[260px] truncate"
                           title={it.is_dead ? it.local_cpa_error : it.status_message}>
                        {it.is_dead ? it.local_cpa_error : it.status_message}
                      </div>
                    )}
                  </td>
                  <td className="text-ink-soft mono text-[12px]">
                    {it.size != null ? `${(it.size / 1024).toFixed(1)} KB` : '—'}
                  </td>
                  <td className="text-ink-soft text-[12px]">
                    {it.updated_at ? relTime(it.updated_at) : '—'}
                  </td>
                  <td>
                    <div className="flex items-center gap-1.5">
                      {it.email && !it.is_dead && (
                        <button
                          type="button"
                          className="btn btn-ghost"
                          style={{ padding: '6px 10px', fontSize: 12 }}
                          onClick={() => reauthOne(it)}
                          disabled={reauthing === it.name || deleting === it.name || bulkBusy}
                          title={it.in_local
                            ? '重跑 OAuth 拿新 token,推回 CPA(本地有密码,直接登录)'
                            : '仅 CPA 号 → 走 email-only OTP 重登(需邮箱域名在 cloud-mail 池)'}
                        >
                          {reauthing === it.name
                            ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                            : <KeyRound className="w-3.5 h-3.5 text-info" />}
                          <span>重认证</span>
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn btn-ghost"
                        style={{ padding: '6px 10px', fontSize: 12 }}
                        onClick={() => deleteOne(it)}
                        disabled={deleting === it.name || reauthing === it.name || bulkBusy}
                        title="从 CPA 删除该 auth-file(本地 DB 保留)"
                      >
                        {deleting === it.name
                          ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                          : <Trash2 className="w-3.5 h-3.5 text-danger" />}
                        <span>删除</span>
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="text-[12px] text-ink-faint flex items-start gap-2">
        <FilterIcon className="w-3 h-3 shrink-0 mt-0.5" />
        <span>
          点击统计卡切换筛选;选中后可「重新认证」(走 OAuth 拿新 token 推回 CPA)或「删除」。
          本地号用密码登录,仅 CPA 号走 email-only OTP(需邮箱域名在 cloud-mail 池)。删除仅作用于 CPA 远端。
        </span>
      </div>
    </div>
  )
}

function StatCard({
  label, value, icon, tone, active, onClick,
}: {
  label: string
  value: number
  icon: React.ReactNode
  tone: 'info' | 'success' | 'muted' | 'danger'
  active: boolean
  onClick: () => void
}) {
  const colors: Record<string, string> = {
    info: 'rgba(0,114,255,0.15)',
    success: 'rgba(16,185,129,0.15)',
    muted: 'rgba(100,116,139,0.15)',
    danger: 'rgba(239,68,68,0.15)',
  }
  const fg: Record<string, string> = {
    info: 'var(--info)',
    success: 'var(--success)',
    muted: 'var(--ink-soft)',
    danger: 'var(--danger)',
  }
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        'card text-left transition-all duration-150 ' +
        (active
          ? '!border-brand-1 ring-2 ring-brand-1/30 shadow-glow'
          : 'hover:!border-line-strong hover:-translate-y-[1px]')
      }
      style={{ padding: '14px 16px' }}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-[12px] text-ink-soft">{label}</span>
        <div
          className="w-7 h-7 rounded-[8px] grid place-items-center"
          style={{ background: colors[tone], color: fg[tone] }}
        >
          {icon}
        </div>
      </div>
      <div className="text-[24px] font-extrabold tracking-tight mono">{value}</div>
    </button>
  )
}

function relTime(iso: string): string {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const diff = Date.now() - t
  if (diff < 60_000) return '刚刚'
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)} 小时前`
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}
