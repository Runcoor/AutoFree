import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard, Rocket, Users, AlertCircle, Settings, LogOut,
} from 'lucide-react'
import clsx from 'clsx'
import { useAuth } from '../store/auth'

const items = [
  { to: '/dashboard', label: '概览', icon: LayoutDashboard },
  { to: '/batch', label: '注册批次', icon: Rocket },
  { to: '/accounts', label: '账号', icon: Users },
  { to: '/pending', label: '待办', icon: AlertCircle },
  { to: '/settings', label: '设置', icon: Settings },
]

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { logout } = useAuth()

  return (
    <div className="h-full flex flex-col">
      <div className="px-6 pt-7 pb-6">
        <div className="text-[20px] font-semibold tracking-tight">AutoFree</div>
        <div className="text-caption text-ink-muted mt-0.5">批量注册 · 自动同步</div>
      </div>

      <nav className="flex-1 px-3 space-y-0.5">
        {items.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            onClick={onNavigate}
            className={({ isActive }) =>
              clsx(
                'flex items-center gap-3 px-3 py-2.5 rounded-btn transition-colors text-[15px]',
                isActive
                  ? 'bg-accent-subtle text-accent font-medium'
                  : 'text-ink-soft hover:bg-line/40 hover:text-ink',
              )
            }
          >
            <Icon className="w-[18px] h-[18px]" strokeWidth={1.75} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="p-3 border-t border-line">
        <button
          onClick={async () => { await logout(); window.location.href = '/login' }}
          className="w-full flex items-center gap-3 px-3 py-2.5 rounded-btn text-[15px] text-ink-soft hover:bg-line/40 hover:text-ink transition-colors"
        >
          <LogOut className="w-[18px] h-[18px]" strokeWidth={1.75} />
          退出登录
        </button>
      </div>
    </div>
  )
}
