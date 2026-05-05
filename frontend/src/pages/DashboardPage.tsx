import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Users, AlertCircle, CheckCircle2, Activity, ArrowRight } from 'lucide-react'
import { accountsApi, freegenApi, type Account, type Batch, type PendingAccount } from '../api/endpoints'
import { Card, Pill } from '../components/ui'

export function DashboardPage() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [pending, setPending] = useState<PendingAccount[]>([])
  const [batches, setBatches] = useState<Batch[]>([])
  const [total, setTotal] = useState(0)

  useEffect(() => {
    accountsApi.list({ page: 1, page_size: 5 }).then(r => { setAccounts(r.items); setTotal(r.total) })
    accountsApi.pending().then(setPending)
    freegenApi.batches(5).then(setBatches)
  }, [])

  const today = new Date(); today.setHours(0, 0, 0, 0)
  const todayCount = accounts.filter(a => a.created_at && new Date(a.created_at) >= today).length
  const cpaSyncedRate = accounts.length > 0
    ? `${Math.round(accounts.filter(a => a.cpa_synced).length / accounts.length * 100)}%`
    : '—'

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-display">概览</h1>
        <p className="text-ink-soft mt-1">AutoFree 工作台</p>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={Users} label="账号总数" value={total.toString()} accent="accent" />
        <StatCard icon={Activity} label="今日新增" value={todayCount.toString()} accent="success" />
        <StatCard icon={AlertCircle} label="待处理 (Pending)" value={pending.length.toString()} accent="warning" />
        <StatCard icon={CheckCircle2} label="CPA 同步率" value={cpaSyncedRate} accent="accent" />
      </div>

      <Card>
        <div className="flex items-center justify-between px-6 pt-5 pb-3">
          <div className="text-title">最近批次</div>
          <Link to="/batch" className="btn-ghost">前往批次 <ArrowRight className="w-3.5 h-3.5" /></Link>
        </div>
        <div className="px-2 pb-2">
          {batches.length === 0
            ? <div className="px-4 py-8 text-center text-ink-muted">暂无批次</div>
            : (
              <ul className="divide-y divide-line">
                {batches.map(b => (
                  <li key={b.id} className="px-4 py-3 flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-medium truncate">{b.id}</div>
                      <div className="text-caption text-ink-muted truncate">
                        @{b.domain} · 计划 {b.count} · {b.created_at ? new Date(b.created_at).toLocaleString('zh-CN') : ''}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <Pill tone="success">{b.ok} 成</Pill>
                      {b.failed > 0 && <Pill tone="danger">{b.failed} 败</Pill>}
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

function StatCard({ icon: Icon, label, value, accent }: {
  icon: any; label: string; value: string; accent: 'accent' | 'success' | 'warning' | 'danger'
}) {
  const colors: Record<string, string> = {
    accent: 'bg-accent/10 text-accent',
    success: 'bg-success/10 text-success',
    warning: 'bg-warning/10 text-warning',
    danger: 'bg-danger/10 text-danger',
  }
  return (
    <Card hover className="p-5">
      <div className={`inline-flex items-center justify-center w-10 h-10 rounded-xl ${colors[accent]}`}>
        <Icon className="w-5 h-5" strokeWidth={2} />
      </div>
      <div className="mt-3 text-[28px] font-semibold leading-none">{value}</div>
      <div className="mt-1 text-caption text-ink-soft">{label}</div>
    </Card>
  )
}
