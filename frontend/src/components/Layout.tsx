import { useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Menu } from 'lucide-react'
import { Sidebar } from './Sidebar'
import { accountsApi, freegenApi } from '../api/endpoints'

export function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [counts, setCounts] = useState<{ batches: number; accounts: number; pending: number }>({
    batches: 0,
    accounts: 0,
    pending: 0,
  })
  const loc = useLocation()

  // Refresh sidebar badges on route change (cheap, gives a live feel)
  useEffect(() => {
    let cancelled = false
    Promise.all([
      accountsApi.list({ page: 1, page_size: 1 }).then((r) => r.total).catch(() => 0),
      accountsApi.pending().then((r) => r.length).catch(() => 0),
      freegenApi.batches(50).then((r) => r.length).catch(() => 0),
    ]).then(([accounts, pending, batches]) => {
      if (cancelled) return
      setCounts({ accounts, pending, batches })
    })
    return () => {
      cancelled = true
    }
  }, [loc.pathname])

  // Auto-close mobile drawer on navigation
  useEffect(() => {
    setMobileOpen(false)
  }, [loc.pathname])

  return (
    <div className="relative z-[1] grid lg:grid-cols-[252px_1fr] min-h-screen">
      {/* Desktop sidebar */}
      <div className="hidden lg:block">
        <div className="sticky top-0 h-screen z-10">
          <Sidebar counts={counts} />
        </div>
      </div>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="lg:hidden fixed inset-0 z-40">
          <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={() => setMobileOpen(false)} />
          <div className="absolute top-0 left-0 h-full w-[260px]">
            <Sidebar counts={counts} onNavigate={() => setMobileOpen(false)} />
          </div>
        </div>
      )}

      {/* Main */}
      <main className="min-w-0 relative">
        {/* Mobile top bar */}
        <header className="lg:hidden sticky top-0 z-30 glass border-b border-line">
          <div className="flex items-center px-4 h-14 gap-3">
            <button
              onClick={() => setMobileOpen(true)}
              className="p-2 -ml-2 rounded-[10px] hover:bg-[var(--row-hover)]"
              aria-label="打开菜单"
            >
              <Menu className="w-5 h-5" />
            </button>
            <div className="font-extrabold tracking-tight">AutoFree</div>
          </div>
        </header>

        <div className="px-6 lg:px-11 py-7 lg:py-9 pb-16 max-w-[1400px]">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
