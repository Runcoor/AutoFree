import { useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Lock, Sun, Moon } from 'lucide-react'
import { Button, Input, useToast } from '../components/ui'
import { useAuth } from '../store/auth'
import { useTheme } from '../store/theme'

export function LoginPage() {
  const [pw, setPw] = useState('')
  const [busy, setBusy] = useState(false)
  const { login } = useAuth()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const next = params.get('next') || '/dashboard'
  const push = useToast((s) => s.push)
  const { theme, toggle } = useTheme()

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!pw || busy) return
    setBusy(true)
    try {
      await login(pw)
      nav(next, { replace: true })
    } catch (err: any) {
      push(err?.response?.data?.detail || '登录失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative z-[1] min-h-screen flex flex-col items-center justify-center px-6">
      <button
        type="button"
        onClick={toggle}
        className="absolute top-6 right-6 btn btn-ghost"
        aria-label="切换主题"
      >
        {theme === 'dark' ? <Moon className="w-4 h-4" /> : <Sun className="w-4 h-4" />}
        {theme === 'dark' ? '暗黑' : '日间'}
      </button>

      <div className="w-full max-w-[400px] page">
        <div className="text-center mb-8">
          <div className="relative inline-flex items-center justify-center w-16 h-16 rounded-2xl grad-bg text-white shadow-[0_10px_30px_rgba(0,114,255,0.35)] mb-5 overflow-hidden">
            <Lock strokeWidth={2.2} className="w-7 h-7 relative z-10" />
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
          <h1 className="text-[36px] font-extrabold tracking-tight grad-text leading-none">AutoFree</h1>
          <p className="text-ink-soft mt-3 text-[14px]">批量注册 · 自动同步 CPA</p>
        </div>

        <form onSubmit={onSubmit} className="card p-7 space-y-5">
          <Input
            type="password"
            label="访问密码"
            placeholder="输入访问密码"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            autoFocus
            autoComplete="current-password"
          />
          <Button type="submit" variant="primary" loading={busy} className="w-full !h-11">
            登录
          </Button>
          <p className="text-[12px] text-ink-faint text-center leading-relaxed">
            首次登录请使用 <code className="px-1.5 py-0.5 bg-bg-soft rounded mono text-[11.5px]">.env</code> 中的{' '}
            <code className="mono text-[11.5px]">APP_PASSWORD</code>
          </p>
        </form>
      </div>
    </div>
  )
}
