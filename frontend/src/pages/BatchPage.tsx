import { useEffect, useRef, useState } from 'react'
import { Play, Square, RefreshCw, Filter, Sparkles, Check, Cloud, Pause, Trash2, ChevronDown, AlertCircle, Clock, ChevronRight, Terminal, Camera, Mail, Smartphone } from 'lucide-react'
import { accountsApi, domainsApi, freegenApi, type Batch, type BatchDetail, type FreegenStatus } from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, LiveDot, Pill, ProgressBar, useToast } from '../components/ui'
import { ScreenshotsModal } from '../components/ScreenshotsModal'

interface SseEvent { ts: number; stage: string; [k: string]: any }

const PRESETS = [10, 30, 50, 100]
const REG_MODE_KEY = 'autofree.regMode'

function readRegMode(): 'email' | 'phone' {
  try {
    const v = localStorage.getItem(REG_MODE_KEY)
    if (v === 'phone' || v === 'email') return v
  } catch {}
  return 'email'
}

export function BatchPage() {
  const [count, setCount] = useState(50)
  const [domain, setDomain] = useState('')
  const [domains, setDomains] = useState<string[]>([])
  const [status, setStatus] = useState<FreegenStatus | null>(null)
  const [history, setHistory] = useState<Batch[]>([])
  const [busy, setBusy] = useState(false)
  const [regMode, setRegMode] = useState<'email' | 'phone'>(() => readRegMode())
  const push = useToast((s) => s.push)
  const evtRef = useRef<EventSource | null>(null)

  // 持久化注册方式选择
  useEffect(() => {
    try { localStorage.setItem(REG_MODE_KEY, regMode) } catch {}
  }, [regMode])

  useEffect(() => { refreshAll() }, [])

  async function refreshAll() {
    const [doms, st, hist] = await Promise.all([
      domainsApi.list().then((rs) => rs.filter((d) => d.enabled).map((d) => d.domain)),
      freegenApi.status().catch(() => ({} as FreegenStatus)),
      freegenApi.batches(20),
    ])
    setDomains(doms)
    setStatus(st && Object.keys(st).length === 0 ? null : st)
    setHistory(hist)
  }

  const [pushingBatch, setPushingBatch] = useState<string | null>(null)
  const [deletingBatch, setDeletingBatch] = useState<string | null>(null)
  const [expandedBatch, setExpandedBatch] = useState<string | null>(null)
  const [details, setDetails] = useState<Record<string, BatchDetail | 'loading'>>({})
  const [logExpanded, setLogExpanded] = useState(false)
  const [showShots, setShowShots] = useState(false)

  async function pushBatch(b: Batch) {
    if (b.status !== 'finished' && b.status !== 'stopped') {
      push('只能推送已完成 / 已停止的批次', 'danger')
      return
    }
    setPushingBatch(b.id)
    try {
      const r = await accountsApi.syncBatch(b.id)
      const tone = r.failed === 0 ? 'success' : r.pushed > 0 ? 'neutral' : 'danger'
      push(`批次 ${b.id} · 共 ${r.total},推 ${r.pushed},失败 ${r.failed},跳过 ${r.skipped}`, tone as any)
      // Refresh detail so cpa_pushed counts update
      const fresh = await freegenApi.batchDetail(b.id)
      setDetails((d) => ({ ...d, [b.id]: fresh }))
    } catch (err: any) {
      push(err?.response?.data?.detail || '推送失败', 'danger')
    } finally {
      setPushingBatch(null)
    }
  }

  async function toggleExpand(b: Batch) {
    if (expandedBatch === b.id) {
      setExpandedBatch(null)
      return
    }
    setExpandedBatch(b.id)
    if (!details[b.id]) {
      setDetails((d) => ({ ...d, [b.id]: 'loading' }))
      try {
        const det = await freegenApi.batchDetail(b.id)
        setDetails((d) => ({ ...d, [b.id]: det }))
      } catch (err: any) {
        push(err?.response?.data?.detail || '加载详情失败', 'danger')
        setDetails((d) => { const c = { ...d }; delete c[b.id]; return c })
      }
    }
  }

  async function deleteBatch(b: Batch) {
    if (b.status === 'running') {
      push('运行中的批次不能删除', 'danger')
      return
    }
    if (!confirm(`删除批次 ${b.id}?(连带账号 / 待办 / 磁盘目录)`)) return
    setDeletingBatch(b.id)
    try {
      await freegenApi.deleteBatch(b.id)
      push(`已删除批次 ${b.id}`, 'success')
      if (expandedBatch === b.id) setExpandedBatch(null)
      setHistory((h) => h.filter((x) => x.id !== b.id))
      setDetails((d) => { const c = { ...d }; delete c[b.id]; return c })
    } catch (err: any) {
      push(err?.response?.data?.detail || '删除失败', 'danger')
    } finally {
      setDeletingBatch(null)
    }
  }

  // Polling 兜底 — SSE 偶尔丢 close 事件,UI 会卡住。每 4s 轮询一次 /status,
  // 后端返 {} 或 stage in finished/stopped/failed 时同步刷新历史
  useEffect(() => {
    if (!status?.task_id) return
    if (['finished', 'stopped', 'failed'].includes(status.stage || '')) return

    let cancelled = false
    const tick = async () => {
      if (cancelled) return
      try {
        const fresh = await freegenApi.status()
        const live = fresh && Object.keys(fresh).length > 0 ? fresh : null
        // 后端没任务了 → UI 也清掉
        if (!live) {
          setStatus(null)
          freegenApi.batches(20).then(setHistory).catch(() => {})
          return
        }
        // 后端进了终态 → 用最新状态盖一下,顺便刷历史
        if (['finished', 'stopped', 'failed'].includes(live.stage || '')) {
          setStatus(live)
          freegenApi.batches(20).then(setHistory).catch(() => {})
          return
        }
        // 还在跑 — 用最新数据 merge,补 SSE 可能漏的字段
        setStatus((prev) => ({
          ...(prev || {}),
          ...live,
          // 保留 SSE 累积的事件,polling 接口 events 列表也同步过来
          events: live.events && live.events.length > (prev?.events?.length || 0) ? live.events : prev?.events,
        }))
      } catch {}
    }
    const t = setInterval(tick, 4000)
    return () => { cancelled = true; clearInterval(t) }
  }, [status?.task_id, status?.stage])

  // Hook SSE if a task is live
  useEffect(() => {
    if (!status?.task_id) return
    if (['finished', 'stopped', 'failed'].includes(status.stage || '')) return
    if (evtRef.current) return

    const es = new EventSource(`/api/sse/task/${status.task_id}`, { withCredentials: true } as any)
    evtRef.current = es

    es.addEventListener('snapshot', (e: any) => {
      try { setStatus(JSON.parse(e.data)) } catch {}
    })
    es.addEventListener('account_started', (e: any) => mergeEvent(e))
    es.addEventListener('account_done', (e: any) => mergeEvent(e))
    es.addEventListener('started', (e: any) => mergeEvent(e))
    es.addEventListener('finished', (e: any) => mergeEvent(e))
    es.addEventListener('stopped', (e: any) => mergeEvent(e))
    es.addEventListener('close', () => {
      es.close()
      evtRef.current = null
      freegenApi.status().then((s) => setStatus(Object.keys(s).length === 0 ? null : s))
      freegenApi.batches(20).then(setHistory)
    })

    return () => { es.close(); evtRef.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.task_id])

  function mergeEvent(e: MessageEvent) {
    try {
      const data: SseEvent = JSON.parse(e.data)
      setStatus((prev) => {
        const events = [...(prev?.events || []), data].slice(-50)
        const next = { ...(prev || {}), events, stage: data.stage }
        if (data.stage === 'account_started' && data.email) {
          next.current_email = data.email
          next.index = data.index ?? next.index
        }
        if (data.stage === 'account_done') {
          if (data.ok) next.ok = (next.ok || 0) + 1
          else next.failed = (next.failed || 0) + 1
          next.index = data.index ?? next.index
        }
        return next
      })
    } catch {}
  }

  async function start() {
    if (busy) return
    setBusy(true)
    try {
      let mode: 'fixed' | 'rotate' | 'random' = 'rotate'
      let dom: string | undefined
      if (domain === '__random__') mode = 'random'
      else if (domain) { mode = 'fixed'; dom = domain }
      const r = await freegenApi.start(count, dom, mode, regMode)
      const domLabel = mode === 'random'
        ? `随机域名(${r.random_pool.length} 个候选)`
        : `@${r.domain}`
      const regLabel = regMode === 'phone' ? '📱手机号' : '📧邮箱'
      push(`已启动批次 ${r.batch_id} · ${regLabel} · ${domLabel} · ${r.count} 个号`, 'success')
      const st = await freegenApi.status()
      setStatus(st)
    } catch (err: any) {
      push(err?.response?.data?.detail || '启动失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    try {
      await freegenApi.stop()
      push('已请求停止 · 当前账号结束后停止', 'neutral')
    } catch (err: any) {
      push(err?.response?.data?.detail || '停止失败', 'danger')
    }
  }

  const running = !!status?.task_id && !['finished', 'stopped', 'failed'].includes(status.stage || '')
  const total = status?.total || count
  const done = (status?.ok || 0) + (status?.failed || 0)

  return (
    <div className="page">
      <div className="flex flex-wrap items-start justify-between gap-4 mb-7">
        <div>
          <h1 className="text-[32px] font-extrabold tracking-[-0.02em] leading-[1.1] m-0">注册批次</h1>
          <p className="text-ink-soft text-[14px] mt-1.5">配置数量与域名 · 启动一次串行批量注册</p>
        </div>
      </div>

      {/* New batch */}
      <Card className="mb-5 anim-in relative">
        <div
          className="absolute inset-0 grad-bg-soft pointer-events-none transition-opacity duration-300"
          style={{ opacity: running ? 1 : 0 }}
        />
        <CardHeader
          title={
            <span className="flex items-center gap-2.5">
              <span className="grad-text inline-flex items-center"><Sparkles size={18} /></span>
              新建批次
            </span>
          }
          subtitle={running ? '已有任务在运行 · 请等待结束或先停止' : '选择域名与数量 · 点击开始'}
          action={
            running && (
              <Pill tone="info">
                <LiveDot tone="info" />
                正在注册
              </Pill>
            )
          }
        />
        <CardBody className="relative">
          {/* 注册方式切换:邮箱(原有) / 手机号(新增) */}
          <div className="field mb-4">
            <label className="min-h-[18px]">注册方式</label>
            <div className="inline-flex bg-bg-soft border border-line rounded-[12px] p-1 gap-1">
              <button
                type="button"
                className={
                  'flex items-center gap-1.5 px-4 h-[34px] rounded-[10px] text-[13px] font-medium transition ' +
                  (regMode === 'email'
                    ? 'grad-bg text-white shadow-glow'
                    : 'text-ink-soft hover:text-ink')
                }
                onClick={() => setRegMode('email')}
                disabled={running}
                title="临时邮箱注册 · 收 OTP · 走原有稳定路径"
              >
                <Mail className="w-3.5 h-3.5" />
                邮箱
              </button>
              <button
                type="button"
                className={
                  'flex items-center gap-1.5 px-4 h-[34px] rounded-[10px] text-[13px] font-medium transition ' +
                  (regMode === 'phone'
                    ? 'grad-bg text-white shadow-glow'
                    : 'text-ink-soft hover:text-ink')
                }
                onClick={() => setRegMode('phone')}
                disabled={running}
                title="SMS 接码注册 · 同号 2 条 SMS 共用 · 国家从接码服务配置读"
              >
                <Smartphone className="w-3.5 h-3.5" />
                手机号
              </button>
            </div>
            {regMode === 'phone' && (
              <div className="text-[11.5px] text-ink-faint mt-1.5 leading-relaxed">
                ⚠ 需在「设置 → 接码服务」配好 5sim / hero-sms key,国家从那里读
                · 每号约消耗 1 个 SMS 订单(2 条短信)
              </div>
            )}
          </div>

          <div className="grid gap-4 grid-cols-1 md:grid-cols-[1fr_1fr_auto]">
            <div className="field">
              <div className="flex items-center justify-between gap-2 min-h-[18px]">
                <label className="!m-0">数量</label>
                <div className="flex flex-wrap gap-1">
                  {PRESETS.map((n) => (
                    <button
                      key={n}
                      type="button"
                      className={
                        'px-2 h-[22px] rounded-full border text-[11.5px] font-medium transition ' +
                        (count === n
                          ? 'grad-bg text-white border-transparent shadow-glow'
                          : 'bg-bg-soft border-line text-ink-soft hover:text-ink hover:border-line-strong')
                      }
                      onClick={() => setCount(n)}
                      disabled={running}
                    >
                      {n}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="btn btn-icon !w-[42px] !h-[42px]"
                  onClick={() => setCount(Math.max(1, count - 5))}
                  disabled={running}
                  aria-label="减少"
                >
                  −
                </button>
                <input
                  className="input mono text-center font-bold !text-[16px]"
                  type="number"
                  min={1}
                  max={500}
                  value={count}
                  onChange={(e) => setCount(Math.max(1, Math.min(500, +e.target.value || 1)))}
                  disabled={running}
                />
                <button
                  type="button"
                  className="btn btn-icon !w-[42px] !h-[42px]"
                  onClick={() => setCount(Math.min(500, count + 5))}
                  disabled={running}
                  aria-label="增加"
                >
                  +
                </button>
              </div>
            </div>

            <div className="field">
              <label className="min-h-[18px]">域名（轮询 = 整批同域 · 随机 = 每号一个）</label>
              <select
                className="select"
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                disabled={running}
              >
                <option value="">自动轮询 (整批同域)</option>
                <option value="__random__">随机 (每号从启用域名抽)</option>
                {domains.map((d) => (
                  <option key={d} value={d}>@{d}</option>
                ))}
              </select>
            </div>

            <div className="field">
              <label className="min-h-[18px] opacity-0 select-none" aria-hidden>·</label>
              {running ? (
                <Button variant="danger" onClick={stop} className="!h-[42px] !px-5 whitespace-nowrap">
                  <Square className="w-3.5 h-3.5" />
                  停止
                </Button>
              ) : (
                <Button
                  variant="primary"
                  onClick={start}
                  loading={busy}
                  disabled={domains.length === 0}
                  className="!h-[42px] !px-6 whitespace-nowrap"
                >
                  <Play className="w-3.5 h-3.5" />
                  开始注册
                </Button>
              )}
            </div>
          </div>
          {domains.length === 0 && !running && (
            <div className="text-[12px] text-warn mt-2">域名池为空 · 请先到设置页添加</div>
          )}

          {(running || done > 0 || (status?.events?.length ?? 0) > 0) && status && (
            <div className="mt-5">
              <div className="flex items-center justify-between mb-2">
                <span className="text-[13px] text-ink-soft">实时进度</span>
                <span className="mono text-[13px] font-bold grad-text">
                  {done} / {total}
                </span>
              </div>
              <ProgressBar value={done} total={total || 1} />
              {status?.current_email && (
                <div className="mt-2 text-[12px] text-ink-soft">
                  当前: <span className="mono text-ink">{status.current_email}</span>
                  {status.stage && (
                    <span className="ml-2 text-ink-faint">· stage=<span className="mono">{status.stage}</span></span>
                  )}
                </div>
              )}

              {/* 折叠日志 */}
              <div className="mt-3 border-t border-line pt-3">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setLogExpanded((v) => !v)}
                    className="flex items-center gap-2 text-[12.5px] text-ink-soft hover:text-ink transition flex-1 min-w-0"
                  >
                    <ChevronRight
                      className={
                        'w-3.5 h-3.5 transition-transform shrink-0 ' +
                        (logExpanded ? 'rotate-90' : '')
                      }
                    />
                    <Terminal className="w-3.5 h-3.5 shrink-0" />
                    <span className="shrink-0">实时日志 ({status.events?.length || 0} 条)</span>
                    {!logExpanded && status.events && status.events.length > 0 && (
                      <span className="ml-2 mono text-[11.5px] text-ink-faint truncate min-w-0">
                        {(() => {
                          const last = status.events[status.events.length - 1] as any
                          const detail = last?.error || last?.email || last?.msg || ''
                          return `${last?.stage || ''} ${detail}`.trim()
                        })()}
                      </span>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowShots(true)}
                    className="btn btn-ghost shrink-0"
                    style={{ padding: '4px 10px', fontSize: 12 }}
                    title="查看浏览器截图(stage 命名,显示当前最新状态)"
                  >
                    <Camera className="w-3.5 h-3.5" />
                    <span>截图</span>
                  </button>
                </div>

                {logExpanded && (
                  <div className="mt-2 bg-bg-soft border border-line rounded-[12px] px-3 py-2.5 max-h-[320px] overflow-y-auto mono text-[12px] leading-relaxed">
                    {(!status.events || status.events.length === 0) && (
                      <div className="text-ink-faint py-1">等待事件…</div>
                    )}
                    {(status.events || []).slice().reverse().map((e, i) => {
                      const ev = e as any
                      const ok = ev?.ok === true
                      const fail = ev?.ok === false
                      return (
                        <div key={i} className="flex gap-2.5 py-0.5">
                          <span className="text-ink-faint shrink-0">
                            {new Date((ev?.ts || 0) * 1000).toLocaleTimeString('zh-CN')}
                          </span>
                          <span
                            className={
                              'shrink-0 font-semibold ' +
                              (fail ? 'text-danger' : ok ? 'text-success' : 'text-brand-1')
                            }
                          >
                            {ev?.stage}
                          </span>
                          <span className="text-ink truncate">
                            {ev?.email || ev?.error || ev?.msg || ev?.cpa_msg || ''}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>
          )}
        </CardBody>
      </Card>

      {/* History */}
      <Card className="anim-in" style={{ animationDelay: '80ms' }}>
        <CardHeader
          title="历史批次"
          subtitle={`${history.length} 个批次`}
          action={
            <div className="flex gap-2">
              <Button variant="ghost" disabled>
                <Filter className="w-3.5 h-3.5" />
                筛选
              </Button>
              <Button variant="ghost" onClick={refreshAll}>
                <RefreshCw className="w-3.5 h-3.5" />
                刷新
              </Button>
            </div>
          }
        />
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>批次 ID</th>
                <th>数量</th>
                <th>域名</th>
                <th>结果</th>
                <th>状态</th>
                <th>时间</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {history.length === 0 && (
                <tr>
                  <td colSpan={7}>
                    <div className="empty-state">
                      <div className="empty-icon"><Sparkles size={22} /></div>
                      暂无批次 — 上方启动一个新批次开始
                    </div>
                  </td>
                </tr>
              )}
              {history.map((b) => {
                const isExpanded = expandedBatch === b.id
                const det = details[b.id]
                return (
                  <>
                    <tr
                      key={b.id}
                      className="cursor-pointer"
                      onClick={() => toggleExpand(b)}
                    >
                      <td>
                        <div className="flex items-center gap-2">
                          <ChevronDown
                            className={
                              'w-3.5 h-3.5 text-ink-faint transition-transform ' +
                              (isExpanded ? 'rotate-0' : '-rotate-90')
                            }
                          />
                          <span className="mono font-semibold">{b.id}</span>
                        </div>
                      </td>
                      <td>{b.count}</td>
                      <td className="text-ink-soft mono">
                        {(() => {
                          const isPhone = b.domain.startsWith('phone:') || b.domain === 'phone'
                          const inner = b.domain.replace(/^phone:?/, '')
                          const tag = isPhone ? <span className="mr-1">📱</span> : null
                          if (!inner || inner === 'phone') {
                            return <>{tag}{isPhone ? '手机号' : '—'}</>
                          }
                          if (inner === 'random') return <>{tag}🎲 随机</>
                          return <>{tag}@{inner}</>
                        })()}
                      </td>
                      <td>
                        <span className="text-success font-semibold">{b.ok}</span>
                        <span className="text-ink-faint"> · </span>
                        <span className={(b.failed ? 'text-danger' : 'text-ink-faint') + ' font-semibold'}>{b.failed}</span>
                      </td>
                      <td>
                        {b.status === 'running' ? (
                          <Pill tone="info">
                            <LiveDot tone="info" />
                            运行中
                          </Pill>
                        ) : b.status === 'finished' ? (
                          <Pill tone="success">
                            <Check className="w-3 h-3" />
                            完成
                          </Pill>
                        ) : b.status === 'stopped' ? (
                          <Pill tone="muted">
                            <Pause className="w-3 h-3" />
                            已停止
                          </Pill>
                        ) : (
                          <Pill tone="muted">{b.status}</Pill>
                        )}
                      </td>
                      <td className="text-ink-soft">{relTime(b.created_at)}</td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-1.5">
                          <button
                            className="btn btn-ghost"
                            style={{ padding: '6px 10px', fontSize: 12 }}
                            onClick={() => pushBatch(b)}
                            disabled={pushingBatch === b.id || b.ok === 0 || b.status === 'running'}
                            title={b.ok === 0 ? '该批次没有成功的账号可推' : '自动 refresh 后整批推到 CPA'}
                          >
                            {pushingBatch === b.id
                              ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                              : <Cloud className="w-3.5 h-3.5" />}
                            推 CPA
                          </button>
                          <button
                            className="btn btn-ghost btn-icon"
                            onClick={() => deleteBatch(b)}
                            disabled={deletingBatch === b.id || b.status === 'running'}
                            title={b.status === 'running' ? '运行中的批次不能删除' : '删除批次 + 该批账号 + 硬盘目录'}
                          >
                            {deletingBatch === b.id
                              ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                              : <Trash2 className="w-3.5 h-3.5 text-danger" />}
                          </button>
                        </div>
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr>
                        <td colSpan={7} className="!p-0">
                          <BatchDetailPanel detail={det} batch={b} />
                        </td>
                      </tr>
                    )}
                  </>
                )
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <ScreenshotsModal open={showShots} onClose={() => setShowShots(false)} />
    </div>
  )
}

function BatchDetailPanel({
  detail,
  batch,
}: {
  detail: BatchDetail | 'loading' | undefined
  batch: Batch
}) {
  if (!detail) return null
  if (detail === 'loading') {
    return (
      <div className="px-6 py-5 bg-bg-soft border-t border-line text-[13px] text-ink-soft">
        加载详情中…
      </div>
    )
  }

  const { summary, accounts, pending, results } = detail

  // 把 results 的每条按 ok 分类:成功(已写 account)、注册成功但 OAuth 没过(进 pending)、注册阶段就败的(没存)
  const inAccount = new Set(accounts.map((a) => a.email))
  const inPending = new Set(pending.map((p) => p.email))
  const dropped = results.filter((r) => !r.ok && r.email && !inPending.has(r.email))

  return (
    <div className="px-6 py-5 bg-bg-soft border-t border-line space-y-5">
      {/* Summary chips */}
      <div className="flex flex-wrap items-center gap-2.5 text-[12.5px]">
        <Pill tone="muted">总数 {summary.total}</Pill>
        <Pill tone="success">成功 {summary.ok}</Pill>
        <Pill tone="danger">失败 {summary.failed}</Pill>
        <Pill tone="info">CPA 已推 {summary.cpa_pushed}</Pill>
        {summary.cpa_unpushed > 0 && (
          <Pill tone="warn">CPA 未推 {summary.cpa_unpushed}</Pill>
        )}
        <Pill tone="warn">待办 {summary.pending}</Pill>
        <span className="ml-auto text-ink-faint mono text-[11.5px]">
          batch_id={batch.id}
        </span>
      </div>

      {/* 成功的 account */}
      {accounts.length > 0 && (
        <div>
          <div className="text-[12.5px] font-semibold text-ink-soft mb-2 flex items-center gap-2">
            <Check className="w-3.5 h-3.5 text-success" />
            成功账号 · {accounts.length}
          </div>
          <div className="bg-bg-elev border border-line rounded-[10px] divide-y divide-line">
            {accounts.map((a) => (
              <div key={a.email} className="flex items-center gap-3 px-3.5 py-2.5 text-[13px]">
                <span className="mono truncate flex-1">{a.email}</span>
                <Pill tone="muted">{a.plan_type || 'free'}</Pill>
                {a.cpa_synced ? (
                  <Pill tone="success"><Check className="w-3 h-3" />CPA 已推</Pill>
                ) : a.cpa_error ? (
                  <Pill tone="danger" className="max-w-[280px] !flex-shrink"><AlertCircle className="w-3 h-3" />{(a.cpa_error || '').slice(0, 40)}</Pill>
                ) : (
                  <Pill tone="warn"><Clock className="w-3 h-3" />未推</Pill>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 进 pending 的(可继续验证) */}
      {pending.length > 0 && (
        <div>
          <div className="text-[12.5px] font-semibold text-ink-soft mb-2 flex items-center gap-2">
            <Clock className="w-3.5 h-3.5 text-warn" />
            待办 · 注册成功但 OAuth/phone 没过 · {pending.length}
            <span className="text-ink-faint font-normal text-[11px]">(到「待办」页可点继续验证)</span>
          </div>
          <div className="bg-bg-elev border border-line rounded-[10px] divide-y divide-line">
            {pending.map((p) => (
              <div key={p.email} className="flex items-center gap-3 px-3.5 py-2.5 text-[13px]">
                <span className="mono truncate flex-1 max-w-[280px]" title={p.email}>{p.email}</span>
                <Pill tone="warn">{p.error_kind || 'unknown'}</Pill>
                <span className="text-ink-faint truncate max-w-[400px] text-[12px]" title={p.error}>
                  {p.error}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 注册阶段就败的(邮箱删了,无法继续) */}
      {dropped.length > 0 && (
        <div>
          <div className="text-[12.5px] font-semibold text-ink-soft mb-2 flex items-center gap-2">
            <AlertCircle className="w-3.5 h-3.5 text-danger" />
            注册阶段失败 · 邮箱已删 · {dropped.length}
            <span className="text-ink-faint font-normal text-[11px]">(无法继续,需重起新批)</span>
          </div>
          <div className="bg-bg-elev border border-line rounded-[10px] divide-y divide-line">
            {dropped.map((r, i) => (
              <div key={`${r.email}-${i}`} className="flex items-center gap-3 px-3.5 py-2.5 text-[13px]">
                <span className="text-ink-faint w-8">#{r.index}</span>
                <span className="mono truncate flex-1 text-ink-soft max-w-[260px]" title={r.email}>{r.email}</span>
                <Pill tone="muted">{r.error_kind || 'unknown'}</Pill>
                <span className="text-ink-faint truncate max-w-[420px] text-[12px]" title={r.error}>
                  {(r.error || '').replace(/^[a-z_]+: /, '')}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {results.length === 0 && accounts.length === 0 && pending.length === 0 && (
        <div className="text-[13px] text-ink-faint py-2">
          未找到该批次的 results.json — 可能批次目录已删,或太老的批次。
        </div>
      )}
    </div>
  )
}

function relTime(iso: string | null): string {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)} 小时前`
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}
