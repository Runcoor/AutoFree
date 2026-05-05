import { useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Lock } from 'lucide-react'
import { Button, Input, useToast } from '../components/ui'
import { useAuth } from '../store/auth'

export function LoginPage() {
  const [pw, setPw] = useState('')
  const [busy, setBusy] = useState(false)
  const { login } = useAuth()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const next = params.get('next') || '/dashboard'
  const push = useToast(s => s.push)

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
    <div className="min-h-screen flex flex-col items-center justify-center px-6 bg-bg">
      <div className="w-full max-w-[400px]">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-accent text-white shadow-md mb-4">
            <Lock strokeWidth={2} className="w-6 h-6" />
          </div>
          <h1 className="text-display">AutoFree</h1>
          <p className="text-ink-soft mt-2 text-[15px]">批量注册 OpenAI free 账号 · 自动同步 CPA</p>
        </div>

        <form onSubmit={onSubmit} className="card p-6 space-y-4">
          <Input
            type="password"
            label="密码"
            placeholder="输入访问密码"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            autoFocus
            autoComplete="current-password"
          />
          <Button type="submit" loading={busy} className="w-full">登录</Button>
          <p className="text-caption text-ink-muted text-center">
            首次登录请使用 <code className="px-1 py-0.5 bg-line/60 rounded">.env</code> 中的 APP_PASSWORD
          </p>
        </form>
      </div>
    </div>
  )
}
