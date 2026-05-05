import { useEffect, useState } from 'react'
import { Trash2, Upload, X } from 'lucide-react'
import { accountsApi, type PendingAccount } from '../api/endpoints'
import { Button, Card, CardHeader, Pill, useToast } from '../components/ui'

export function PendingPage() {
  const [items, setItems] = useState<PendingAccount[]>([])
  const [importing, setImporting] = useState<PendingAccount | null>(null)
  const push = useToast(s => s.push)

  useEffect(() => { refresh() }, [])
  function refresh() { accountsApi.pending().then(setItems) }

  async function remove(p: PendingAccount) {
    if (!confirm(`删除 pending 账号 ${p.email}?(邮箱密码不会找回)`)) return
    await accountsApi.removePending(p.email)
    push('已删除', 'success')
    refresh()
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-display">待办</h1>
        <p className="text-ink-soft mt-1">注册成功但 OAuth 失败的号 · 共 {items.length} 条</p>
      </div>

      <Card>
        <CardHeader title="待处理列表" subtitle="可手动获取 token 后,粘贴 JSON 导入" />
        <div className="overflow-x-auto">
          <table className="w-full text-[14px]">
            <thead className="text-ink-soft text-[12px] uppercase tracking-wider">
              <tr className="border-y border-line">
                <th className="text-left font-medium px-6 py-2.5">邮箱</th>
                <th className="text-left font-medium px-3 py-2.5">密码</th>
                <th className="text-left font-medium px-3 py-2.5">失败原因</th>
                <th className="text-left font-medium px-3 py-2.5">时间</th>
                <th className="text-right font-medium px-6 py-2.5">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {items.length === 0 && (
                <tr><td colSpan={5} className="text-center py-12 text-ink-muted">暂无待办</td></tr>
              )}
              {items.map(p => (
                <tr key={p.id} className="hover:bg-bg/40">
                  <td className="px-6 py-3 font-mono">{p.email}</td>
                  <td className="px-3 py-3 font-mono text-ink-soft">{p.password}</td>
                  <td className="px-3 py-3">
                    <Pill tone="warning">{p.error_kind || 'unknown'}</Pill>
                    <div className="text-caption text-ink-muted mt-1 max-w-md truncate" title={p.error}>{p.error}</div>
                  </td>
                  <td className="px-3 py-3 text-ink-soft">
                    {p.created_at ? new Date(p.created_at).toLocaleString('zh-CN') : '—'}
                  </td>
                  <td className="px-6 py-3 text-right">
                    <div className="inline-flex gap-1">
                      <Button variant="ghost" onClick={() => setImporting(p)}>
                        <Upload className="w-3.5 h-3.5" /> 导入 JSON
                      </Button>
                      <Button variant="ghost" onClick={() => remove(p)}>
                        <Trash2 className="w-3.5 h-3.5" /> 删除
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
  const push = useToast(s => s.push)

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
    <div className="fixed inset-0 z-40 grid place-items-center px-4 bg-black/30" onClick={onClose}>
      <div className="card w-full max-w-[600px] p-6" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="text-title">导入认证 JSON</div>
            <div className="text-caption text-ink-muted mt-1">{pending.email}</div>
          </div>
          <button onClick={onClose} className="p-2 rounded hover:bg-line/40"><X className="w-4 h-4" /></button>
        </div>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={12}
          placeholder='{"access_token":"...","refresh_token":"...","id_token":"...","email":"...",...}'
          className="input-base font-mono text-[13px] leading-relaxed"
        />
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>取消</Button>
          <Button onClick={submit} loading={busy}>导入</Button>
        </div>
      </div>
    </div>
  )
}
