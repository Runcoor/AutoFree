import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import { Menu } from 'lucide-react'
import { Sidebar } from './Sidebar'

export function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="min-h-screen flex">
      {/* Desktop sidebar */}
      <aside className="hidden lg:block w-[240px] shrink-0">
        <div className="fixed top-0 left-0 h-screen w-[240px] border-r border-line bg-surface">
          <Sidebar />
        </div>
      </aside>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="lg:hidden fixed inset-0 z-40">
          <div className="absolute inset-0 bg-black/30" onClick={() => setMobileOpen(false)} />
          <div className="absolute top-0 left-0 h-full w-[260px] bg-surface shadow-lg">
            <Sidebar onNavigate={() => setMobileOpen(false)} />
          </div>
        </div>
      )}

      {/* Main */}
      <main className="flex-1 min-w-0">
        {/* Mobile top bar */}
        <header className="lg:hidden sticky top-0 z-30 bg-bg/80 backdrop-blur border-b border-line">
          <div className="flex items-center px-4 h-14">
            <button
              onClick={() => setMobileOpen(true)}
              className="p-2 -ml-2 rounded-btn hover:bg-line/50"
              aria-label="打开菜单"
            >
              <Menu className="w-5 h-5" />
            </button>
            <div className="ml-2 font-semibold tracking-tight">AutoFree</div>
          </div>
        </header>

        <div className="max-w-[1100px] mx-auto px-6 lg:px-8 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
