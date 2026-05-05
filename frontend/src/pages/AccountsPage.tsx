import { useEffect, useState } from 'react'
import { Search, Download, Copy, Eye, EyeOff, ExternalLink, Check, Clock, AlertCircle, Sparkles, Cloud, RefreshCw, ChevronLeft, ChevronRight } from 'lucide-react'
import { accountsApi, type Account } from '../api/endpoints'
import { Button, Card, CardHeader, Pill, Segmented, useToast } from '../components/ui'

type Filter = 'all' | 'synced' | 'unsynced'

export function AccountsPage() {
  const [items, setItems] = useState<Account[]>([])
  const [page, setPage] = useState(1)
  const [pageSize] = useState(50)
  const [total, setTotal] = useState(0)
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const [show, setShow] = useState<Record<number, boolean>>({})
  const push = useToast((s) => s.push)

  useEffect(() => {
    accountsApi
      .list({
        page,
        page_size: pageSize,
        cpa_synced: filter === 'all' ? undefined : filter === 'synced',
      })
      .then((r) => { setItems(r.items); setTotal(r.total) })
  }, [page, pageSize, filter])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const filtered = search ? items.filter((a) => a.email.includes(search)) : items

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
      <div className="flex flex-wrap items-start justify-between gap-4 mb-7">
        <div>
          <h1 className="text-[32px] font-extrabold tracking-[-0.02em] leading-[1.1] m-0">账号</h1>
          <p className="text-ink-soft text-[14px] mt-1.5">已注册成功的账号 · 共 {total} 个</p>
        </div>
        <div className="flex items-center gap-2">
          <Button disabled title="批量导出待实现">
            <Download className="w-3.5 h-3.5" />
            导出 JSON
          </Button>
          <Button variant="primary" disabled title="批量同步 CPA 已可在 批次页 → 历史 → 推 CPA 操作">
            <Cloud className="w-3.5 h-3.5" />
            同步 CPA
          </Button>
        </div>
      </div>

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
              <Segmented<Filter>
                value={filter}
                onChange={(v) => { setFilter(v); setPage(1) }}
                options={[
                  { value: 'all', label: '全部' },
                  { value: 'synced', label: '已同步' },
                  { value: 'unsynced', label: '未同步' },
                ]}
              />
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
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        title="自动 refresh 后推 CPA"
                        className="btn btn-ghost btn-icon"
                        disabled={pushingId === a.id}
                        onClick={() => pushOne(a)}
                      >
                        {pushingId === a.id
                          ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                          : <Cloud className="w-3.5 h-3.5" />}
                      </button>
                      <a
                        href={accountsApi.download(a.email)}
                        title="下载 auth.json"
                        className="btn btn-ghost btn-icon"
                      >
                        <Download className="w-3.5 h-3.5" />
                      </a>
                      <a
                        href={`https://chat.openai.com/`}
                        target="_blank"
                        rel="noreferrer"
                        title="在 ChatGPT 打开"
                        className="btn btn-ghost btn-icon"
                      >
                        <ExternalLink className="w-3.5 h-3.5" />
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
    </div>
  )
}
