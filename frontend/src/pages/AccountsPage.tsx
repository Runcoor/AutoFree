import { useEffect, useState } from 'react'
import { Download, Copy, ChevronLeft, ChevronRight } from 'lucide-react'
import { accountsApi, type Account } from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, Pill, useToast } from '../components/ui'

export function AccountsPage() {
  const [items, setItems] = useState<Account[]>([])
  const [page, setPage] = useState(1)
  const [pageSize] = useState(50)
  const [total, setTotal] = useState(0)
  const [filter, setFilter] = useState<'all' | 'synced' | 'unsynced'>('all')
  const push = useToast(s => s.push)

  useEffect(() => {
    accountsApi.list({
      page, page_size: pageSize,
      cpa_synced: filter === 'all' ? undefined : filter === 'synced',
    }).then(r => { setItems(r.items); setTotal(r.total) })
  }, [page, pageSize, filter])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-display">账号</h1>
        <p className="text-ink-soft mt-1">已注册成功的账号 · 共 {total} 个</p>
      </div>

      <Card>
        <CardHeader
          title="账号列表"
          action={
            <div className="flex gap-1.5">
              {[
                { v: 'all', l: '全部' },
                { v: 'synced', l: '已同步' },
                { v: 'unsynced', l: '未同步' },
              ].map(o => (
                <button
                  key={o.v}
                  onClick={() => { setPage(1); setFilter(o.v as any) }}
                  className={`px-3 py-1.5 text-[13px] rounded-btn transition-colors
                              ${filter === o.v ? 'bg-accent text-white' : 'bg-line/40 text-ink-soft hover:bg-line/60'}`}
                >{o.l}</button>
              ))}
            </div>
          }
        />
        <div className="overflow-x-auto">
          <table className="w-full text-[14px]">
            <thead className="text-ink-soft text-[12px] uppercase tracking-wider">
              <tr className="border-y border-line">
                <th className="text-left font-medium px-6 py-2.5">邮箱</th>
                <th className="text-left font-medium px-3 py-2.5">密码</th>
                <th className="text-left font-medium px-3 py-2.5">Plan</th>
                <th className="text-left font-medium px-3 py-2.5">CPA</th>
                <th className="text-left font-medium px-3 py-2.5">创建时间</th>
                <th className="text-right font-medium px-6 py-2.5">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {items.length === 0 && (
                <tr><td colSpan={6} className="text-center py-12 text-ink-muted">暂无数据</td></tr>
              )}
              {items.map(a => (
                <tr key={a.id} className="hover:bg-bg/40">
                  <td className="px-6 py-3 font-mono">{a.email}</td>
                  <td className="px-3 py-3 font-mono text-ink-soft">{a.password}</td>
                  <td className="px-3 py-3"><Pill tone="accent">{a.plan_type}</Pill></td>
                  <td className="px-3 py-3">
                    {a.cpa_synced
                      ? <Pill tone="success">已同步</Pill>
                      : a.cpa_error
                        ? <Pill tone="danger" >失败</Pill>
                        : <Pill tone="neutral">未启用</Pill>}
                  </td>
                  <td className="px-3 py-3 text-ink-soft">
                    {a.created_at ? new Date(a.created_at).toLocaleString('zh-CN') : '—'}
                  </td>
                  <td className="px-6 py-3 text-right">
                    <div className="inline-flex gap-1">
                      <button
                        title="复制 邮箱:密码"
                        onClick={() => {
                          navigator.clipboard?.writeText(`${a.email}:${a.password}`)
                          push('已复制', 'success')
                        }}
                        className="p-1.5 rounded hover:bg-line/40"
                      >
                        <Copy className="w-4 h-4" />
                      </button>
                      <a
                        href={accountsApi.download(a.email)}
                        title="下载 auth.json"
                        className="p-1.5 rounded hover:bg-line/40 text-ink"
                      >
                        <Download className="w-4 h-4" />
                      </a>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <CardBody className="flex items-center justify-between">
          <div className="text-caption text-ink-muted">第 {page} / {totalPages} 页</div>
          <div className="flex gap-1.5">
            <Button variant="secondary" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>
              <ChevronLeft className="w-4 h-4" /> 上一页
            </Button>
            <Button variant="secondary" disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>
              下一页 <ChevronRight className="w-4 h-4" />
            </Button>
          </div>
        </CardBody>
      </Card>
    </div>
  )
}
