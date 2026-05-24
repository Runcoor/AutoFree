import { useEffect, useMemo, useRef, useState } from 'react'
import { Trash2, KeyRound, Clock, RefreshCw, Upload, X, Check, AlertCircle, Play, Square, Zap, Copy } from 'lucide-react'
import { accountsApi, freegenApi, type FreegenStatus, type PendingAccount } from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, LiveDot, Pill, ProgressBar, Textarea, useToast } from '../components/ui'

type PendingTab = 'all' | 'paid' | 'unpaid'

export function PendingPage() {
  const [items, setItems] = useState<PendingAccount[]>([])
  const [importing, setImporting] = useState<PendingAccount | null>(null)
  const [bulk, setBulk] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const [tab, setTab] = useState<PendingTab>('all')
  // 表示「我刚点了哪个号的继续验证」 — 仅用于 API 提交瞬间的 loading,不依赖它判断 spinner
  const [submittingEmail, setSubmittingEmail] = useState<string | null>(null)
  const [resumeAllBusy, setResumeAllBusy] = useState(false)
  const [status, setStatus] = useState<FreegenStatus | null>(null)
  const push = useToast((s) => s.push)
  const evtRef = useRef<EventSource | null>(null)

  const paidCount = items.filter((p) => p.phone_verified).length
  const unpaidCount = items.length - paidCount

  const filtered = useMemo(() => {
    let xs = items
    if (tab === 'paid') xs = xs.filter((p) => p.phone_verified)
    else if (tab === 'unpaid') xs = xs.filter((p) => !p.phone_verified)
    // 已付费的优先排前(避免被忽略漏掉)
    return [...xs].sort((a, b) => {
      if (a.phone_verified !== b.phone_verified) return a.phone_verified ? -1 : 1
      return (b.created_at || '').localeCompare(a.created_at || '')
    })
  }, [items, tab])

  useEffect(() => { refresh() }, [])
  function refresh() {
    accountsApi.pending().then(setItems)
    freegenApi.status().then((s) => setStatus(s && Object.keys(s).length === 0 ? null : s)).catch(() => {})
  }

  async function remove(p: PendingAccount) {
    if (!confirm(`删除 pending ${p.email}?`)) return
    await accountsApi.removePending(p.email)
    push('已删除', 'success')
    refresh()
  }

  async function resume(p: PendingAccount) {
    if (!confirm(`继续验证 ${p.email}?(消耗 SMS 余额)`)) return
    setSubmittingEmail(p.email)
    try {
      const r = await freegenApi.resume(p.email)
      push(`已启动 resume task=${r.task_id}`, 'success')
      const s = await freegenApi.status()
      setStatus(s && Object.keys(s).length === 0 ? null : s)
    } catch (err: any) {
      push(err?.response?.data?.detail || '启动失败', 'danger')
    } finally {
      setSubmittingEmail(null)
    }
  }

  async function resumeAll() {
    if (filtered.length === 0) {
      push('当前筛选下无可继续验证的号', 'danger')
      return
    }
    const scopeLabel = tab === 'paid' ? '已付费 ' : tab === 'unpaid' ? '未付费 ' : ''
    if (!confirm(`继续验证${scopeLabel}${filtered.length} 个?(消耗 SMS 余额)`)) return
    setResumeAllBusy(true)
    try {
      // tab === 'all' 时不传 emails(后端走全量),否则只传 filtered 的 emails
      const emails = tab === 'all' ? undefined : filtered.map((p) => p.email)
      const r = await freegenApi.resumeAll(emails)
      push(`已启动批量 resume · ${r.total} 个号串行跑${r.skipped_no_password ? ` · 跳过 ${r.skipped_no_password} 个缺密码` : ''}`, 'success')
      const s = await freegenApi.status()
      setStatus(s && Object.keys(s).length === 0 ? null : s)
    } catch (err: any) {
      push(err?.response?.data?.detail || '启动失败', 'danger')
    } finally {
      setResumeAllBusy(false)
    }
  }

  async function stop() {
    try {
      await freegenApi.stop()
      push('已请求停止', 'neutral')
    } catch (err: any) {
      push(err?.response?.data?.detail || '停止失败', 'danger')
    }
  }

  // SSE on resume task
  useEffect(() => {
    if (!status?.task_id) return
    if (['finished', 'stopped', 'failed'].includes(status.stage || '')) return
    if (evtRef.current) return

    const es = new EventSource(`/api/sse/task/${status.task_id}`, { withCredentials: true } as any)
    evtRef.current = es
    const merge = (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data)
        setStatus((prev) => {
          const events = [...(prev?.events || []), data].slice(-50)
          const next = { ...(prev || {}), events, stage: data.stage }
          if (data.stage === 'account_started' && data.email) {
            next.current_email = data.email
            if (typeof data.outer_index === 'number') next.index = data.outer_index
          }
          if (data.stage === 'account_done') {
            if (data.ok) next.ok = (next.ok || 0) + 1
            else next.failed = (next.failed || 0) + 1
            if (typeof data.outer_index === 'number') next.index = data.outer_index
          }
          return next
        })
      } catch {}
    }
    es.addEventListener('snapshot', (e: any) => { try { setStatus(JSON.parse(e.data)) } catch {} })
    ;['account_started', 'account_done', 'started', 'finished', 'stopped'].forEach((n) =>
      es.addEventListener(n, merge as any),
    )
    es.addEventListener('close', () => {
      es.close()
      evtRef.current = null
      refresh()
    })
    return () => { es.close(); evtRef.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.task_id])

  const resumeRunning =
    !!status?.task_id &&
    !['finished', 'stopped', 'failed'].includes(status.stage || '')

  async function bulkImport() {
    let arr: any
    try { arr = JSON.parse(bulk) } catch { return push('JSON 解析失败', 'danger') }
    if (!Array.isArray(arr)) return push('请粘贴一个 JSON 数组', 'danger')
    setBulkBusy(true)
    let ok = 0, fail = 0
    try {
      for (const obj of arr) {
        const email = obj?.email
        if (!email) { fail++; continue }
        try { await accountsApi.manualImport(email, obj); ok++ } catch { fail++ }
      }
      push(`导入完成 · 成功 ${ok} · 失败 ${fail}`, ok > 0 ? 'success' : 'danger')
      if (ok > 0) { setBulk(''); refresh() }
    } finally {
      setBulkBusy(false)
    }
  }

  return (
    <div className="page">
      <div className="flex flex-wrap items-start justify-between gap-4 mb-7">
        <div>
          <h1 className="text-[32px] font-extrabold tracking-[-0.02em] leading-[1.1] m-0">待办</h1>
          <p className="text-ink-soft text-[14px] mt-1.5">
            注册成功但 OAuth 失败的号 · 共 {items.length} 条
          </p>
        </div>
        <div className="flex items-center gap-2">
          {resumeRunning && (
            <Button variant="danger" onClick={stop}>
              <Square className="w-3.5 h-3.5" />
              停止
            </Button>
          )}
          {!resumeRunning && filtered.length > 0 && (
            <Button
              variant="primary"
              onClick={resumeAll}
              loading={resumeAllBusy}
              disabled={resumeAllBusy}
              title={
                tab === 'all'
                  ? '按队列串行重跑全部 pending(无密码自动走邮箱 OTP)'
                  : tab === 'paid'
                    ? '只跑已付费的号(💰)— 这些 OpenAI 端 phone 已绑,通常不烧 5sim'
                    : '只跑未付费的号 — 可能撞 phone gate 触发 5sim 救援'
              }
            >
              <Zap className="w-3.5 h-3.5" />
              {tab === 'all' ? '一键继续全部' : tab === 'paid' ? '继续已付费' : '继续未付费'}
              <span className="mono ml-1 opacity-80">({filtered.length})</span>
            </Button>
          )}
          <Button onClick={refresh}>
            <RefreshCw className="w-3.5 h-3.5" />
            刷新
          </Button>
        </div>
      </div>

      {/* Resume in progress card */}
      {resumeRunning && status && (
        <Card className="anim-in mb-5">
          <CardHeader
            title={
              <span className="flex items-center gap-2">
                继续验证中
                <span className="mono text-[13px] text-ink-soft">{status.current_email}</span>
              </span>
            }
            subtitle={`stage=${status.stage}`}
            action={
              <Pill tone="info">
                <LiveDot tone="info" />
                运行中
              </Pill>
            }
          />
          <CardBody>
            <ProgressBar value={(status.ok || 0) + (status.failed || 0)} total={status.total || 1} />
            <div className="mt-2 text-[12px] text-ink-soft">
              {status.events && status.events.length > 0 && (
                <span className="mono">
                  {(status.events.at(-1)?.stage || '')}{' '}
                  {(status.events.at(-1) as any)?.error || (status.events.at(-1) as any)?.email || ''}
                </span>
              )}
            </div>
          </CardBody>
        </Card>
      )}

      <Card className="anim-in mb-5">
        <CardHeader
          title={
            <span className="flex items-center gap-2">
              待处理列表
              <Pill tone="muted">{filtered.length} / {items.length}</Pill>
            </span>
          }
          subtitle="💰 = SMS 已扣费,此号已通过手机验证 — 优先 resume,绝不能丢"
          action={
            <div className="flex items-center gap-1 p-1 bg-bg-soft rounded-[10px] border border-line">
              <TabBtn active={tab === 'all'} onClick={() => setTab('all')} label="全部" count={items.length} />
              <TabBtn active={tab === 'paid'} onClick={() => setTab('paid')} label="💰 已付费" count={paidCount} tone="info" />
              <TabBtn active={tab === 'unpaid'} onClick={() => setTab('unpaid')} label="未付费" count={unpaidCount} />
            </div>
          }
        />
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>邮箱</th>
                <th>密码</th>
                <th>手机号</th>
                <th>失败原因</th>
                <th>时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={6}>
                    <div className="empty-state">
                      <div className="empty-icon"><Check size={22} /></div>
                      {items.length === 0
                        ? '暂无待办 — 所有账号 OAuth 都成功了'
                        : tab === 'paid' ? '没有已付费但未完成的号'
                          : '当前筛选条件下无结果'}
                    </div>
                  </td>
                </tr>
              )}
              {filtered.map((p) => {
                // 只有「正在跑这个号」才显示 spinner;其他行只是 disabled(队列等待)
                const isRunningThis = resumeRunning && status?.current_email === p.email
                const isSubmittingThis = submittingEmail === p.email
                const isWaitingInQueue = resumeRunning && !isRunningThis
                let title = '打开浏览器登录该号 → 重跑 phone gate → 推 CPA'
                if (!p.password) title = '无密码 → 走邮箱 OTP 登录(从 cloud-mail 取验证码)'
                else if (isRunningThis) title = '当前正在跑这个号'
                else if (isWaitingInQueue) title = '已有 resume 在跑,等结束(或一键全部时排队中)'
                return (
                <tr
                  key={p.id}
                  className={isRunningThis ? 'bg-bg-soft' : ''}
                  style={p.phone_verified ? { background: 'rgba(0,114,255,0.05)' } : undefined}
                >
                  <td>
                    <div className="flex items-center gap-2.5">
                      <div
                        className="w-8 h-8 rounded-[8px] grid place-items-center shrink-0"
                        style={{
                          background: p.phone_verified ? 'rgba(0,114,255,0.18)' : 'rgba(245,158,11,0.15)',
                          color: p.phone_verified ? 'var(--info)' : 'var(--warn)',
                        }}
                      >
                        {isRunningThis
                          ? <RefreshCw className="w-3.5 h-3.5 animate-spin" style={{ color: 'var(--info)' }} />
                          : p.phone_verified ? <span style={{ fontSize: 14, lineHeight: 1 }}>💰</span>
                            : <Clock className="w-3.5 h-3.5" />}
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className="mono text-[13px] truncate max-w-[240px]" title={p.email}>{p.email}</span>
                          {p.phone_verified && (
                            <span
                              title={`SMS 已扣费,此号已通过手机验证${p.phone_verified_at ? ' · ' + new Date(p.phone_verified_at).toLocaleString('zh-CN') : ''}`}
                            >
                              <Pill tone="info">💰 已付费</Pill>
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td>
                    <span className="mono text-[13px]">{p.password}</span>
                  </td>
                  <td>
                    {p.phone_e164 ? (
                      <div className="flex items-center gap-1.5">
                        <span
                          className="mono text-[12.5px]"
                          style={{ color: p.phone_verified ? 'var(--info)' : 'var(--ink-soft)' }}
                          title={p.phone_verified
                            ? `此号 SMS 已扣费 — 可用 ${p.phone_e164} + 密码登录 chatgpt.com 手动补救`
                            : `手机号:${p.phone_e164}`}
                        >
                          {p.phone_e164}
                        </span>
                        <button
                          type="button"
                          className="btn btn-ghost btn-icon"
                          style={{ width: 24, height: 24 }}
                          onClick={() => {
                            navigator.clipboard?.writeText(p.phone_e164)
                            push('已复制手机号', 'success')
                          }}
                          aria-label="复制手机号"
                          title="复制"
                        >
                          <Copy className="w-3 h-3" />
                        </button>
                      </div>
                    ) : (
                      <span className="text-ink-faint">—</span>
                    )}
                  </td>
                  <td>
                    <Pill tone="warn">{p.error_kind || 'unknown'}</Pill>
                    <div className="text-[12px] text-ink-faint mt-1.5 max-w-md truncate" title={p.error}>
                      {p.error}
                    </div>
                  </td>
                  <td className="text-ink-soft">{p.created_at ? new Date(p.created_at).toLocaleString('zh-CN') : '—'}</td>
                  <td>
                    <div className="flex items-center gap-1.5 whitespace-nowrap">
                      <button
                        type="button"
                        className="btn btn-primary whitespace-nowrap"
                        style={{ padding: '6px 12px', fontSize: 12 }}
                        onClick={() => resume(p)}
                        disabled={isSubmittingThis || resumeRunning}
                        title={title}
                      >
                        {(isRunningThis || isSubmittingThis)
                          ? <RefreshCw className="w-3 h-3 animate-spin shrink-0" />
                          : <Play className="w-3 h-3 shrink-0" />}
                        <span>{isRunningThis ? '运行中' : isWaitingInQueue ? '等待中' : '继续验证'}</span>
                      </button>
                      <button
                        type="button"
                        className="btn whitespace-nowrap"
                        style={{ padding: '6px 12px', fontSize: 12 }}
                        onClick={() => setImporting(p)}
                        title="如果你已经手动跑通了 OAuth,粘贴 token JSON 直接导入"
                      >
                        <KeyRound className="w-3 h-3 shrink-0" />
                        <span>手动导入</span>
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-icon"
                        onClick={() => remove(p)}
                        title="删除"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </td>
                </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card className="anim-in" style={{ animationDelay: '80ms' }}>
        <CardHeader
          icon={<Upload size={18} />}
          title="批量导入 JSON"
          subtitle="粘贴一个数组,每个对象包含 email 和完整的 codex auth JSON"
        />
        <CardBody>
          <Textarea
            rows={6}
            value={bulk}
            onChange={(e) => setBulk(e.target.value)}
            placeholder={'[\n  { "email": "...", "access_token": "...", "refresh_token": "...", ... },\n  ...\n]'}
          />
          <div className="flex justify-end gap-2 mt-3">
            <Button onClick={() => setBulk('')} disabled={!bulk}>
              清空
            </Button>
            <Button variant="primary" onClick={bulkImport} loading={bulkBusy} disabled={!bulk.trim()}>
              <Check className="w-3.5 h-3.5" />
              导入并同步
            </Button>
          </div>
        </CardBody>
      </Card>

      {importing && (
        <ImportModal
          pending={importing}
          onClose={() => setImporting(null)}
          onDone={() => { setImporting(null); refresh() }}
        />
      )}
    </div>
  )
}

function TabBtn({
  active, onClick, label, count, tone,
}: {
  active: boolean; onClick: () => void; label: string; count: number; tone?: 'info'
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        'px-3 py-1.5 rounded-[8px] text-[12.5px] font-medium transition flex items-center gap-1.5 ' +
        (active
          ? (tone === 'info' ? 'bg-info text-white' : 'grad-bg text-white shadow-glow')
          : 'text-ink-soft hover:text-ink')
      }
      style={active && tone === 'info' ? { background: 'var(--info)', color: 'white' } : undefined}
    >
      <span>{label}</span>
      <span
        className={
          'mono text-[11px] px-1.5 rounded-full ' +
          (active ? 'bg-white/20' : 'bg-bg text-ink-faint')
        }
      >
        {count}
      </span>
    </button>
  )
}

function ImportModal({ pending, onClose, onDone }: {
  pending: PendingAccount; onClose: () => void; onDone: () => void
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const push = useToast((s) => s.push)

  async function submit() {
    let json: any
    try { json = JSON.parse(text) }
    catch { return push('JSON 解析失败', 'danger') }
    setBusy(true)
    try {
      await accountsApi.manualImport(pending.email, json)
      push('已导入', 'success')
      onDone()
    } catch (err: any) {
      push(err?.response?.data?.detail || '导入失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="card-header">
          <div>
            <h3>导入认证 JSON</h3>
            <div className="sub mono">{pending.email}</div>
          </div>
          <button onClick={onClose} className="btn btn-ghost btn-icon" aria-label="关闭">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="card-body">
          <Textarea
            rows={12}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={'{"access_token":"...","refresh_token":"...","id_token":"...","email":"...",...}'}
          />
          <div className="flex justify-end gap-2 mt-4">
            <Button onClick={onClose}>取消</Button>
            <Button variant="primary" onClick={submit} loading={busy}>
              <Check className="w-3.5 h-3.5" />
              导入
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
