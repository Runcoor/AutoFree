import { useEffect, useRef, useState } from 'react'
import { Play, Square, RefreshCw, Filter, Sparkles, Check, Cloud, Pause } from 'lucide-react'
import { accountsApi, domainsApi, freegenApi, type Batch, type FreegenStatus } from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, LiveDot, Pill, ProgressBar, useToast } from '../components/ui'

interface SseEvent { ts: number; stage: string; [k: string]: any }

const PRESETS = [10, 30, 50, 100]

export function BatchPage() {
  const [count, setCount] = useState(50)
  const [domain, setDomain] = useState('')
  const [domains, setDomains] = useState<string[]>([])
  const [status, setStatus] = useState<FreegenStatus | null>(null)
  const [history, setHistory] = useState<Batch[]>([])
  const [busy, setBusy] = useState(false)
  const push = useToast((s) => s.push)
  const evtRef = useRef<EventSource | null>(null)

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
    } catch (err: any) {
      push(err?.response?.data?.detail || '推送失败', 'danger')
    } finally {
      setPushingBatch(null)
    }
  }

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
      const r = await freegenApi.start(count, domain || undefined)
      push(`已启动批次 ${r.batch_id} · @${r.domain} · ${r.count} 个号`, 'success')
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
              <label className="min-h-[18px]">域名（留空 = 自动轮询启用域名）</label>
              <select
                className="select"
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                disabled={running}
              >
                <option value="">自动选择 (轮询)</option>
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

          {(running || done > 0) && (
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
                </div>
              )}
            </div>
          )}
        </CardBody>
      </Card>

      {/* Live event stream when running */}
      {running && status && <EventStreamCard status={status} />}

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
              {history.map((b) => (
                <tr key={b.id}>
                  <td className="mono font-semibold">{b.id}</td>
                  <td>{b.count}</td>
                  <td className="text-ink-soft mono">@{b.domain}</td>
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
                  <td>
                    <button
                      className="btn btn-ghost"
                      style={{ padding: '6px 10px', fontSize: 12 }}
                      onClick={() => pushBatch(b)}
                      disabled={pushingBatch === b.id || b.ok === 0}
                      title={b.ok === 0 ? '该批次没有成功的账号可推' : '自动 refresh 后整批推到 CPA'}
                    >
                      {pushingBatch === b.id
                        ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                        : <Cloud className="w-3.5 h-3.5" />}
                      推 CPA
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}

function EventStreamCard({ status }: { status: FreegenStatus }) {
  const events = (status.events || []).slice(-20).reverse()
  return (
    <Card className="mb-5 anim-in" style={{ animationDelay: '40ms' }}>
      <CardHeader
        title="事件流"
        subtitle={status.task_id ? `task=${status.task_id.slice(0, 12)}…` : ''}
        action={
          <Pill tone="info">
            <LiveDot tone="info" />
            实时
          </Pill>
        }
      />
      <CardBody>
        <div className="bg-bg-soft border border-line rounded-[12px] px-3 py-2.5 max-h-[280px] overflow-y-auto mono text-[12px] leading-relaxed">
          {events.length === 0 && <div className="text-ink-faint py-1">等待事件…</div>}
          {events.map((e, i) => (
            <div key={i} className="flex gap-2.5 py-0.5">
              <span className="text-ink-faint shrink-0">{new Date(e.ts * 1000).toLocaleTimeString('zh-CN')}</span>
              <span className={`shrink-0 font-semibold ${e.ok === false ? 'text-danger' : e.ok ? 'text-success' : 'text-brand-1'}`}>
                {e.stage}
              </span>
              <span className="text-ink truncate">{e.email || e.error || e.msg || ''}</span>
            </div>
          ))}
        </div>
      </CardBody>
    </Card>
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
