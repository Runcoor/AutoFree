import { NavLink } from 'react-router-dom'
import {
  LayoutGrid, Rocket, Users, Inbox, Settings as SettingsIcon, LogOut, Sun, Moon,
} from 'lucide-react'
import clsx from 'clsx'
import { useAuth } from '../store/auth'
import { useTheme } from '../store/theme'

interface Counts {
  batches?: number
  accounts?: number
  pending?: number
}

interface NavItem {
  to: string
  label: string
  icon: typeof LayoutGrid
  key: string
  accent?: boolean
}

const items: readonly NavItem[] = [
  { to: '/dashboard', label: '概览', icon: LayoutGrid, key: 'overview' },
  { to: '/batch', label: '注册批次', icon: Rocket, key: 'batches' },
  { to: '/accounts', label: '账号', icon: Users, key: 'accounts' },
  { to: '/pending', label: '待办', icon: Inbox, key: 'pending', accent: true },
  { to: '/settings', label: '设置', icon: SettingsIcon, key: 'settings' },
]

export function Sidebar({ onNavigate, counts }: { onNavigate?: () => void; counts?: Counts }) {
  const { logout } = useAuth()
  const { theme, toggle } = useTheme()

  const badge = (key: string) => {
    if (key === 'batches') return counts?.batches
    if (key === 'accounts') return counts?.accounts
    if (key === 'pending') return counts?.pending
    return undefined
  }

  return (
    <aside className="sidebar h-full w-full flex flex-col px-3.5 py-5 glass border-r border-line">
      <div className="brand flex items-center gap-3 px-3 pb-5">
        <div className="brand-mark relative w-10 h-10 rounded-[12px] grad-bg grid place-items-center text-white shadow-[0_6px_16px_rgba(0,114,255,0.35)] overflow-hidden">
          <Logo />
          <span
            className="absolute inset-0 pointer-events-none"
            style={{
              background:
                'linear-gradient(135deg, transparent 40%, rgba(255,255,255,0.4) 50%, transparent 60%)',
              animation: 'shine 3s ease-in-out infinite',
              transform: 'translateX(-100%)',
            }}
          />
        </div>
        <div className="leading-tight">
          <div className="font-extrabold text-[16px] tracking-tight text-ink">AutoFree</div>
          <div className="text-[11px] text-ink-soft mt-0.5">批量注册 · 自动同步</div>
        </div>
      </div>

      <nav className="flex flex-col gap-1 flex-1">
        {items.map((it) => {
          const Icon = it.icon
          const b = badge(it.key)
          return (
            <NavLink
              key={it.to}
              to={it.to}
              onClick={onNavigate}
              className={({ isActive }) =>
                clsx(
                  'group relative flex items-center gap-3 px-3.5 py-2.5 rounded-[10px] text-[14px] font-medium border border-transparent transition-all duration-150',
                  isActive
                    ? 'text-white grad-bg shadow-glow'
                    : 'text-ink-soft hover:text-ink hover:bg-[var(--row-hover)]',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <Icon className="w-[18px] h-[18px] shrink-0" strokeWidth={2} />
                  <span className="flex-1 truncate">{it.label}</span>
                  {b != null && (
                    <span
                      className={clsx(
                        'min-w-[22px] text-center text-[11px] font-semibold px-2 py-[2px] rounded-full',
                        isActive
                          ? 'bg-white/20 text-white'
                          : it.accent && (b as number) > 0
                            ? 'bg-warn/15 text-warn'
                            : 'bg-bg-soft text-ink-soft',
                      )}
                    >
                      {b}
                    </span>
                  )}
                </>
              )}
            </NavLink>
          )
        })}
      </nav>

      <div className="pt-3 border-t border-line flex flex-col gap-2">
        <button
          type="button"
          onClick={toggle}
          className="flex items-center justify-between px-3.5 py-2.5 rounded-[10px] bg-bg-soft border border-line text-[13px] text-ink-soft hover:text-ink transition"
        >
          <span className="flex items-center gap-2">
            {theme === 'dark' ? <Moon className="w-4 h-4" /> : <Sun className="w-4 h-4" />}
            <span>{theme === 'dark' ? '暗黑模式' : '日间模式'}</span>
          </span>
          <span className="relative w-9 h-5 rounded-full grad-bg">
            <span
              className="absolute top-[2px] w-4 h-4 rounded-full bg-white shadow"
              style={{ left: theme === 'dark' ? '18px' : '2px', transition: 'left 0.25s cubic-bezier(0.4,0,0.2,1)' }}
            />
          </span>
        </button>
        <button
          type="button"
          onClick={async () => { await logout(); window.location.href = '/login' }}
          className="flex items-center gap-2.5 px-3.5 py-2.5 rounded-[10px] text-[13px] text-ink-soft hover:bg-[var(--row-hover)] hover:text-danger transition"
        >
          <LogOut className="w-4 h-4" />
          <span>退出登录</span>
        </button>
      </div>
    </aside>
  )
}

function Logo() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 19L12 4L19 19" />
      <path d="M8.5 13H15.5" />
    </svg>
  )
}
