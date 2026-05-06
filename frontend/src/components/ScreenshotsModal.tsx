import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Camera, RefreshCw, X, ZoomIn } from 'lucide-react'
import { accountsApi } from '../api/endpoints'

type Item = { name: string; size: number; mtime: number; mtime_iso: string }

export function ScreenshotsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [items, setItems] = useState<Item[]>([])
  const [loading, setLoading] = useState(false)
  const [enlarged, setEnlarged] = useState<string | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    if (open) refresh()
  }, [open])

  async function refresh() {
    setLoading(true)
    setErr('')
    try {
      const r = await accountsApi.screenshots()
      setItems(r.items)
    } catch (e: any) {
      setErr(e?.response?.data?.detail || '加载失败')
    } finally {
      setLoading(false)
    }
  }

  if (!open) return null

  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 1100, width: '90vw' }}
      >
        <div className="card-header">
          <div>
            <h3 className="flex items-center gap-2">
              <Camera className="w-4 h-4" />
              浏览器截图
              {items.length > 0 && (
                <span className="text-[12px] text-ink-faint font-normal">· {items.length} 张</span>
              )}
            </h3>
            <div className="sub">
              按 stage 命名,每个号执行时会覆盖。要看具体某号失败截图,请在它失败后立即查看。
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={refresh} className="btn btn-ghost" disabled={loading}>
              <RefreshCw className={'w-3.5 h-3.5 ' + (loading ? 'animate-spin' : '')} />
              刷新
            </button>
            <button onClick={onClose} className="btn btn-ghost btn-icon" aria-label="关闭">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="card-body" style={{ maxHeight: '70vh', overflowY: 'auto' }}>
          {err && (
            <div className="text-[13px] text-danger mb-3">{err}</div>
          )}
          {!loading && items.length === 0 && !err && (
            <div className="empty-state">
              <div className="empty-icon"><Camera size={22} /></div>
              暂无截图 — 跑一次注册流程会自动生成
            </div>
          )}

          <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))' }}>
            {items.map((it) => {
              const url = accountsApi.screenshotUrl(it.name)
              return (
                <div
                  key={it.name}
                  className="rounded-[10px] border border-line bg-bg-soft overflow-hidden hover:border-line-strong transition cursor-pointer group"
                  onClick={() => setEnlarged(it.name)}
                  title="点击放大"
                >
                  <div className="relative" style={{ aspectRatio: '16/10', background: '#000' }}>
                    <img
                      src={url}
                      alt={it.name}
                      style={{ width: '100%', height: '100%', objectFit: 'contain' }}
                      loading="lazy"
                    />
                    <div className="absolute inset-0 flex items-center justify-center bg-black/0 group-hover:bg-black/30 transition">
                      <ZoomIn className="w-6 h-6 text-white opacity-0 group-hover:opacity-100 transition" />
                    </div>
                  </div>
                  <div className="px-2.5 py-2 text-[11.5px]">
                    <div className="mono truncate font-semibold" title={it.name}>{it.name}</div>
                    <div className="text-ink-faint mt-0.5 flex items-center justify-between">
                      <span>{(it.size / 1024).toFixed(1)} KB</span>
                      <span>{relTime(it.mtime)}</span>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </div>

      {/* 放大层 */}
      {enlarged && (
        <div
          className="fixed inset-0 z-[200] flex items-center justify-center p-6 bg-black/85"
          onClick={() => setEnlarged(null)}
        >
          <img
            src={accountsApi.screenshotUrl(enlarged)}
            alt={enlarged}
            style={{ maxWidth: '95%', maxHeight: '95%', objectFit: 'contain' }}
            onClick={(e) => e.stopPropagation()}
          />
          <button
            onClick={() => setEnlarged(null)}
            className="absolute top-4 right-4 btn btn-ghost btn-icon"
            style={{ background: 'rgba(255,255,255,0.1)', color: 'white' }}
            aria-label="关闭"
          >
            <X className="w-5 h-5" />
          </button>
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 mono text-[12px] text-white/80 bg-black/40 px-3 py-1 rounded-full">
            {enlarged}
          </div>
        </div>
      )}
    </div>,
    document.body,
  )
}

function relTime(epoch: number): string {
  if (!epoch) return ''
  const diff = Date.now() / 1000 - epoch
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}
