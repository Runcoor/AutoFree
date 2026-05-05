import { useEffect, useState } from 'react'
import { Trash2, KeyRound, Clock, RefreshCw, Upload, X, Check, AlertCircle } from 'lucide-react'
import { accountsApi, type PendingAccount } from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, Pill, Textarea, useToast } from '../components/ui'

export function PendingPage() {
  const [items, setItems] = useState<PendingAccount[]>([])
  const [importing, setImporting] = useState<PendingAccount | null>(null)
  const [bulk, setBulk] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const push = useToast((s) => s.push)

  useEffect(() => { refresh() }, [])
  function refresh() { accountsApi.pending().then(setItems) }

  async function remove(p: PendingAccount) {
    if (!confirm(`删除 pending 账号 ${p.email}？(邮箱密码不会找回)`)) return
    await accountsApi.removePending(p.email)
    push('已删除', 'success')
    refresh()
  }

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
          <Button onClick={refresh}>
            <RefreshCw className="w-3.5 h-3.5" />
            刷新
          </Button>
        </div>
      </div>

      <Card className="anim-in mb-5">
        <CardHeader
          title="待处理列表"
          subtitle="可手动获取 token 后,粘贴 JSON 导入"
          action={
            items.length > 0 && (
              <Pill tone="warn">
                <AlertCircle className="w-3 h-3" />
                需要处理
              </Pill>
            )
          }
        />
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>邮箱</th>
                <th>密码</th>
                <th>失败原因</th>
                <th>时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 && (
                <tr>
                  <td colSpan={5}>
                    <div className="empty-state">
                      <div className="empty-icon"><Check size={22} /></div>
                      暂无待办 — 所有账号 OAuth 都成功了
                    </div>
                  </td>
                </tr>
              )}
              {items.map((p) => (
                <tr key={p.id}>
                  <td>
                    <div className="flex items-center gap-2.5">
                      <div className="w-8 h-8 rounded-[8px] grid place-items-center shrink-0" style={{ background: 'rgba(245,158,11,0.15)', color: 'var(--warn)' }}>
                        <Clock className="w-3.5 h-3.5" />
                      </div>
                      <span className="mono text-[13px] truncate max-w-[240px]" title={p.email}>{p.email}</span>
                    </div>
                  </td>
                  <td>
                    <span className="mono text-[13px]">{p.password}</span>
                  </td>
                  <td>
                    <Pill tone="warn">{p.error_kind || 'unknown'}</Pill>
                    <div className="text-[12px] text-ink-faint mt-1.5 max-w-md truncate" title={p.error}>
                      {p.error}
                    </div>
                  </td>
                  <td className="text-ink-soft">{p.created_at ? new Date(p.created_at).toLocaleString('zh-CN') : '—'}</td>
                  <td>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '6px 12px', fontSize: 12 }}
                        onClick={() => setImporting(p)}
                      >
                        <KeyRound className="w-3 h-3" />
                        导入 Token
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
              ))}
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
