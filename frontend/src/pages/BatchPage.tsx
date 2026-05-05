import { useEffect, useRef, useState } from 'react'
import { Play, Square, RotateCcw } from 'lucide-react'
import { domainsApi, freegenApi, type Batch, type FreegenStatus } from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, Input, Pill, ProgressRing, useToast } from '../components/ui'

interface SseEvent { ts: number; stage: string; [k: string]: any }

export function BatchPage() {
  const [count, setCount] = useState(5)
  const [domain, setDomain] = useState('')
  const [domains, setDomains] = useState<string[]>([])
  const [status, setStatus] = useState<FreegenStatus | null>(null)
  const [history, setHistory] = useState<Batch[]>([])
  const [busy, setBusy] = useState(false)
  const push = useToast(s => s.push)
  const evtRef = useRef<EventSource | null>(null)

  // 初始拉一次 status + domain 池 + history
  useEffect(() => {
    refreshAll()
  }, [])

  async function refreshAll() {
    const [doms, st, hist] = await Promise.all([
      domainsApi.list().then(rs => rs.filter(d => d.enabled).map(d => d.domain)),
      freegenApi.status(),
      freegenApi.batches(20),
    ])
    setDomains(doms)
    setStatus(Object.keys(st).length === 0 ? null : st)
    setHistory(hist)
  }

  // 如果有任务在跑且 stage 不是终态,挂 SSE
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
      freegenApi.status().then(s => setStatus(Object.keys(s).length === 0 ? null : s))
      freegenApi.batches(20).then(setHistory)
    })
    es.onerror = () => {
      // 自动重连由浏览器 EventSource 处理
    }

    return () => { es.close(); evtRef.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.task_id])

  function mergeEvent(e: MessageEvent) {
    try {
      const data: SseEvent = JSON.parse(e.data)
      setStatus(prev => {
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
      push(`已启动批次 #${r.batch_id} (域名 @${r.domain}, ${r.count} 个号)`, 'success')
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
      push('已请求停止,当前账号结束后停止', 'neutral')
    } catch (err: any) {
      push(err?.response?.data?.detail || '停止失败', 'danger')
    }
  }

  const running = !!status?.task_id && !['finished', 'stopped', 'failed'].includes(status.stage || '')

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-display">注册批次</h1>
        <p className="text-ink-soft mt-1">配置数量与域名,启动一次串行批量注册</p>
      </div>

      <Card>
        <CardHeader title="新建批次" subtitle={running ? '已有任务在运行,请先等待结束或停止' : '选择域名与数量,点击开始'} />
        <CardBody>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <Input
              label="数量"
              type="number"
              min={1}
              max={200}
              value={count}
              onChange={(e) => setCount(Math.max(1, Math.min(200, Number(e.target.value) || 1)))}
              disabled={running}
            />
            <div className="md:col-span-2">
              <span className="label-base">域名(留空 = 自动轮询选用启用域名)</span>
              <select
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                disabled={running}
                className="input-base"
              >
                <option value="">自动选择 (轮询)</option>
                {domains.map(d => <option key={d} value={d}>@{d}</option>)}
              </select>
              {domains.length === 0 && (
                <span className="text-caption text-warning mt-1.5 block">域名池为空,请先到设置页添加</span>
              )}
            </div>
          </div>
          <div className="mt-5 flex gap-3">
            <Button onClick={start} loading={busy} disabled={running || domains.length === 0}>
              <Play className="w-4 h-4" /> 开始注册
            </Button>
            {running && (
              <Button variant="danger" onClick={stop}>
                <Square className="w-3.5 h-3.5" /> 停止
              </Button>
            )}
          </div>
        </CardBody>
      </Card>

      {status?.task_id && <CurrentTaskCard status={status} />}

      <Card>
        <CardHeader
          title="历史批次"
          action={<Button variant="ghost" onClick={refreshAll}><RotateCcw className="w-3.5 h-3.5" /> 刷新</Button>}
        />
        <div className="px-2 pb-2">
          {history.length === 0
            ? <div className="px-4 py-8 text-center text-ink-muted">暂无批次</div>
            : (
              <ul className="divide-y divide-line">
                {history.map(b => (
                  <li key={b.id} className="px-4 py-3 flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-mono text-[13px]">{b.id}</div>
                      <div className="text-caption text-ink-muted">
                        @{b.domain} · 计划 {b.count} · {b.created_at ? new Date(b.created_at).toLocaleString('zh-CN') : ''}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <Pill tone="success">{b.ok}</Pill>
                      {b.failed > 0 && <Pill tone="danger">{b.failed}</Pill>}
                      <Pill tone={b.status === 'finished' ? 'success' : b.status === 'running' ? 'accent' : 'neutral'}>
                        {b.status}
                      </Pill>
                    </div>
                  </li>
                ))}
              </ul>
            )}
        </div>
      </Card>
    </div>
  )
}

function CurrentTaskCard({ status }: { status: FreegenStatus }) {
  const total = status.total || 0
  const done = (status.ok || 0) + (status.failed || 0)
  const events = (status.events || []).slice(-20).reverse()
  const isFinal = ['finished', 'stopped', 'failed'].includes(status.stage || '')

  return (
    <Card>
      <CardHeader
        title="当前任务"
        subtitle={status.task_id ? `task_id=${status.task_id}` : ''}
        action={<Pill tone={isFinal ? (status.stage === 'finished' ? 'success' : 'neutral') : 'accent'}>{status.stage}</Pill>}
      />
      <CardBody>
        <div className="flex items-center gap-6 mb-5">
          <ProgressRing value={done} total={total} label={<>{done}<span className="text-ink-muted">/{total}</span></>} />
          <div className="space-y-1.5 text-[15px]">
            <div><Pill tone="success">成功 {status.ok || 0}</Pill> <Pill tone="danger">失败 {status.failed || 0}</Pill></div>
            {status.current_email && (
              <div className="text-ink-soft">当前: <span className="font-mono text-ink">{status.current_email}</span></div>
            )}
          </div>
        </div>

        <div className="text-[13px] font-medium text-ink-soft mb-2">事件流</div>
        <div className="bg-bg/70 rounded-input px-3 py-2 max-h-[280px] overflow-y-auto font-mono text-[12px] leading-relaxed">
          {events.length === 0 && <div className="text-ink-muted">暂无事件</div>}
          {events.map((e, i) => (
            <div key={i} className="flex gap-2 py-0.5">
              <span className="text-ink-muted shrink-0">{new Date(e.ts * 1000).toLocaleTimeString('zh-CN')}</span>
              <span className={`shrink-0 ${e.ok === false ? 'text-danger' : e.ok ? 'text-success' : 'text-accent'}`}>
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
