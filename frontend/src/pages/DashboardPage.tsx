import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Users, Activity, AlertCircle, CheckCircle2, RefreshCw, Plus, ArrowRight, Pause, Check } from 'lucide-react'
import { accountsApi, freegenApi, type Account, type Batch, type FreegenStatus, type PendingAccount } from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, Counter, LiveDot, Pill, ProgressBar, Segmented, Sparkline } from '../components/ui'

interface Stat {
  label: string
  value: number
  trend?: string
  decimals?: number
  suffix?: string
  icon: React.ReactNode
  spark: number[]
  down?: boolean
}

export function DashboardPage() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [pending, setPending] = useState<PendingAccount[]>([])
  const [batches, setBatches] = useState<Batch[]>([])
  const [status, setStatus] = useState<FreegenStatus | null>(null)
  const [total, setTotal] = useState(0)
  const [range, setRange] = useState<'7d' | '30d' | 'all'>('7d')
  const [refreshing, setRefreshing] = useState(false)

  async function loadAll() {
    setRefreshing(true)
    try {
      const [list, pend, bs, st] = await Promise.all([
        accountsApi.list({ page: 1, page_size: 50 }),
        accountsApi.pending(),
        freegenApi.batches(20),
        freegenApi.status().catch(() => ({} as FreegenStatus)),
      ])
      setAccounts(list.items)
      setTotal(list.total)
      setPending(pend)
      setBatches(bs)
      setStatus(st && Object.keys(st).length === 0 ? null : st)
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  const today = useMemo(() => {
    const d = new Date(); d.setHours(0, 0, 0, 0)
    return d
  }, [])

  const todayCount = accounts.filter((a) => a.created_at && new Date(a.created_at) >= today).length
  const cpaSyncedRate = accounts.length > 0
    ? (accounts.filter((a) => a.cpa_synced).length / accounts.length) * 100
    : 0

  // Build a 7-day buckets series from accounts.created_at
  const trend = useMemo(() => buildDailyTrend(accounts, range === '7d' ? 7 : range === '30d' ? 30 : 30), [accounts, range])
  const spark7 = trend.slice(-7).map((d) => d.v)
  const trendForChart = trend.slice(-7) // chart always shows last 7 even on '30d' for readability

  const stats: Stat[] = [
    { label: '账号总数', value: total, icon: <Users size={18} />, spark: spark7.length ? spark7 : [4, 5, 6, 8, 9, 11, 12, total || 0] },
    { label: '今日新增', value: todayCount, icon: <Activity size={18} />, spark: spark7.length ? spark7 : [2, 3, 3, 5, 4, 6, 7, todayCount] },
    { label: '待处理', value: pending.length, icon: <AlertCircle size={18} />, spark: [9, 8, 8, 7, 6, 7, 6, pending.length], down: true },
    { label: 'CPA 同步率', value: cpaSyncedRate, decimals: 1, suffix: '%', icon: <CheckCircle2 size={18} />, spark: [94, 95, 96, 97, 98, 98, 99, cpaSyncedRate || 0] },
  ]

  const isRunning = !!status?.task_id && !['finished', 'stopped', 'failed'].includes(status?.stage || '')

  return (
    <div className="page">
      {/* Topbar */}
      <div className="flex flex-wrap items-start justify-between gap-4 mb-7">
        <div>
          <h1 className="text-[32px] font-extrabold tracking-[-0.02em] leading-[1.1] m-0">概览</h1>
          <p className="text-ink-soft text-[14px] mt-1.5">AutoFree 工作台 · 实时监控你的注册管线</p>
        </div>
        <div className="flex items-center gap-2.5">
          <div className="flex items-center gap-2 px-3.5 py-2 bg-bg-elev border border-line rounded-[10px] text-[13px]">
            <LiveDot tone={isRunning ? 'info' : 'success'} />
            <span className="text-ink-soft">{isRunning ? '任务运行中' : '系统正常'}</span>
          </div>
          <Button onClick={loadAll} loading={refreshing}>
            <RefreshCw className="w-3.5 h-3.5" />
            刷新
          </Button>
          <Link to="/batch" className="btn btn-primary">
            <Plus className="w-3.5 h-3.5" />
            新建批次
          </Link>
        </div>
      </div>

      {/* Stats grid */}
      <div className="grid gap-4 mb-5 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
        {stats.map((s, i) => (
          <div key={s.label} className="stat anim-in" style={{ animationDelay: `${i * 60}ms` }}>
            <div className="stat-top">
              <div className="stat-icon">{s.icon}</div>
              {s.trend && <span className={'stat-trend' + (s.down ? ' down' : '')}>{s.trend}</span>}
            </div>
            <div className="stat-value grad-text">
              <Counter value={s.value} decimals={s.decimals || 0} suffix={s.suffix || ''} />
            </div>
            <div className="flex items-center justify-between mt-1.5">
              <div className="stat-label">{s.label}</div>
              <Sparkline data={s.spark} />
            </div>
          </div>
        ))}
      </div>

      {/* Chart + side */}
      <div className="grid gap-4 mb-5 grid-cols-1 xl:grid-cols-[1.6fr_1fr]">
        <Card className="anim-in" style={{ animationDelay: '240ms' }}>
          <CardHeader
            title="注册趋势"
            subtitle={`最近 ${trendForChart.length} 天每日成功注册账号数`}
            action={
              <Segmented<'7d' | '30d' | 'all'>
                value={range}
                onChange={setRange}
                options={[
                  { value: '7d', label: '7 天' },
                  { value: '30d', label: '30 天' },
                  { value: 'all', label: '全部' },
                ]}
              />
            }
          />
          <CardBody className="!pt-3">
            <AreaChart data={trendForChart} />
            <div className="flex justify-between px-3.5 text-[12px] text-ink-faint mt-1">
              {trendForChart.map((d, i) => (
                <span key={i}>{d.l}</span>
              ))}
            </div>
          </CardBody>
        </Card>

        <Card className="anim-in" style={{ animationDelay: '300ms' }}>
          <CardHeader
            title="当前任务"
            action={
              isRunning ? (
                <Pill tone="info">
                  <LiveDot tone="info" />
                  运行中
                </Pill>
              ) : (
                <Pill tone="muted">空闲</Pill>
              )
            }
          />
          <CardBody className="flex flex-col gap-4.5" >
            {isRunning && status ? (
              <>
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-[13px] text-ink-soft">批次 <span className="mono">{status.batch_id || status.task_id?.slice(0, 8)}</span></span>
                    <span className="mono text-[13px] font-semibold">{(status.ok || 0) + (status.failed || 0)} / {status.total || 0}</span>
                  </div>
                  <ProgressBar value={(status.ok || 0) + (status.failed || 0)} total={status.total || 1} />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="p-3.5 bg-bg-soft rounded-[12px]">
                    <div className="text-[11px] text-ink-faint mb-1">本批次成功</div>
                    <div className="text-[22px] font-extrabold grad-text">{status.ok || 0}</div>
                  </div>
                  <div className="p-3.5 bg-bg-soft rounded-[12px]">
                    <div className="text-[11px] text-ink-faint mb-1">失败</div>
                    <div className="text-[22px] font-extrabold text-danger">{status.failed || 0}</div>
                  </div>
                </div>
                <Link to="/batch" className="btn justify-center">
                  <Pause className="w-3.5 h-3.5" />
                  查看详情
                </Link>
              </>
            ) : (
              <div className="flex flex-col items-center justify-center text-center py-6">
                <div className="w-14 h-14 rounded-[14px] grad-bg-soft text-brand-1 grid place-items-center mb-3">
                  <Activity size={22} />
                </div>
                <div className="text-[14px] text-ink-soft mb-3.5">暂无运行中任务</div>
                <Link to="/batch" className="btn btn-primary">
                  <Plus className="w-3.5 h-3.5" />
                  新建批次
                </Link>
              </div>
            )}
          </CardBody>
        </Card>
      </div>

      {/* Recent batches */}
      <Card className="anim-in" style={{ animationDelay: '360ms' }}>
        <CardHeader
          title="最近批次"
          subtitle="最新创建的批次任务及结果"
          action={
            <Link to="/batch" className="btn btn-ghost">
              前往批次
              <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          }
        />
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>批次 ID</th>
                <th>数量</th>
                <th>进度</th>
                <th>结果</th>
                <th>状态</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {batches.length === 0 && (
                <tr>
                  <td colSpan={6}>
                    <div className="empty-state">
                      <div className="empty-icon"><Activity size={22} /></div>
                      暂无批次 — 去新建一个开始
                    </div>
                  </td>
                </tr>
              )}
              {batches.slice(0, 6).map((b) => {
                const done = b.ok + b.failed
                const pct = b.count > 0 ? Math.round((done / b.count) * 100) : 0
                return (
                  <tr key={b.id}>
                    <td className="mono font-semibold">{b.id}</td>
                    <td>{b.count}</td>
                    <td className="w-[220px]">
                      <div className="flex items-center gap-2.5">
                        <ProgressBar value={done} total={b.count || 1} className="flex-1" />
                        <span className="mono text-[12px] text-ink-soft min-w-[36px]">{pct}%</span>
                      </div>
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
                          进行中
                        </Pill>
                      ) : b.status === 'finished' ? (
                        <Pill tone="success">
                          <Check className="w-3 h-3" />
                          完成
                        </Pill>
                      ) : (
                        <Pill tone="muted">{b.status}</Pill>
                      )}
                    </td>
                    <td className="text-ink-soft">{relTime(b.created_at)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}

// ─── Helpers ────────────────────────────────────────────────
function buildDailyTrend(accounts: Account[], days: number): { l: string; v: number }[] {
  const buckets = new Map<string, number>()
  const now = new Date()
  now.setHours(0, 0, 0, 0)
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now)
    d.setDate(now.getDate() - i)
    buckets.set(d.toDateString(), 0)
  }
  for (const a of accounts) {
    if (!a.created_at) continue
    const d = new Date(a.created_at)
    d.setHours(0, 0, 0, 0)
    const key = d.toDateString()
    if (buckets.has(key)) buckets.set(key, (buckets.get(key) || 0) + 1)
  }
  const wd = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
  return [...buckets.entries()].map(([k, v], idx, arr) => {
    const d = new Date(k)
    const isToday = idx === arr.length - 1
    return { l: isToday ? '今日' : wd[d.getDay()], v }
  })
}

function relTime(iso: string | null): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  const diff = Date.now() - t
  if (diff < 60_000) return '刚刚'
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)} 小时前`
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}

function AreaChart({ data }: { data: { l: string; v: number }[] }) {
  if (data.length === 0) return null
  const w = 720
  const h = 200
  const pad = 12
  const max = Math.max(...data.map((d) => d.v), 1)
  const stepX = data.length > 1 ? (w - pad * 2) / (data.length - 1) : 0
  const path = data
    .map((d, i) => {
      const x = pad + i * stepX
      const y = h - pad - (d.v / max) * (h - pad * 2)
      return (i === 0 ? 'M' : 'L') + x + ',' + y
    })
    .join(' ')
  const fill = path + ` L${pad + (data.length - 1) * stepX},${h - pad} L${pad},${h - pad} Z`
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: '100%', height: 200 }}>
      <defs>
        <linearGradient id="areaFill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="#0072ff" stopOpacity="0.32" />
          <stop offset="1" stopColor="#00c6ff" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="areaStroke" x1="0" x2="1">
          <stop offset="0" stopColor="#0072ff" />
          <stop offset="1" stopColor="#00c6ff" />
        </linearGradient>
      </defs>
      {[0, 1, 2, 3].map((i) => (
        <line
          key={i}
          x1={pad}
          x2={w - pad}
          y1={pad + i * ((h - pad * 2) / 3)}
          y2={pad + i * ((h - pad * 2) / 3)}
          stroke="var(--border)"
          strokeDasharray="3 4"
        />
      ))}
      <path d={fill} fill="url(#areaFill)" />
      <path d={path} fill="none" stroke="url(#areaStroke)" strokeWidth="2.4" strokeLinecap="round" />
      {data.map((d, i) => {
        const x = pad + i * stepX
        const y = h - pad - (d.v / max) * (h - pad * 2)
        return <circle key={i} cx={x} cy={y} r="3" fill="white" stroke="url(#areaStroke)" strokeWidth="2" />
      })}
    </svg>
  )
}
