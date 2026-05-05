import { forwardRef, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode } from 'react'
import clsx from 'clsx'

// ─── Card ───────────────────────────────────────────────────
export function Card({ children, className, hover, ...rest }: { children: ReactNode; className?: string; hover?: boolean } & React.HTMLAttributes<HTMLDivElement>) {
  return <div {...rest} className={clsx('card', hover && 'card-hover', className)}>{children}</div>
}

export function CardHeader({ title, subtitle, action, className }: { title: ReactNode; subtitle?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={clsx('flex items-start justify-between gap-4 px-6 pt-5 pb-4', className)}>
      <div>
        <div className="text-title">{title}</div>
        {subtitle && <div className="text-caption text-ink-soft mt-1">{subtitle}</div>}
      </div>
      {action}
    </div>
  )
}

export function CardBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx('px-6 pb-6', className)}>{children}</div>
}

// ─── Button ─────────────────────────────────────────────────
type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
export const Button = forwardRef<HTMLButtonElement, { variant?: Variant; loading?: boolean } & ButtonHTMLAttributes<HTMLButtonElement>>(
  function Button({ variant = 'primary', loading, className, children, disabled, ...rest }, ref) {
    const cls = {
      primary: 'btn-primary',
      secondary: 'btn-secondary',
      ghost: 'btn-ghost',
      danger: 'btn-danger',
    }[variant]
    return (
      <button ref={ref} {...rest} disabled={disabled || loading} className={clsx(cls, className)}>
        {loading && <span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />}
        {children}
      </button>
    )
  },
)

// ─── Input ──────────────────────────────────────────────────
export const Input = forwardRef<HTMLInputElement, { label?: string; hint?: string; error?: string } & InputHTMLAttributes<HTMLInputElement>>(
  function Input({ label, hint, error, className, ...rest }, ref) {
    return (
      <label className="block">
        {label && <span className="label-base">{label}</span>}
        <input ref={ref} {...rest} className={clsx('input-base', error && 'border-danger focus:border-danger focus:ring-danger/15', className)} />
        {hint && !error && <span className="text-caption text-ink-muted mt-1.5 block">{hint}</span>}
        {error && <span className="text-caption text-danger mt-1.5 block">{error}</span>}
      </label>
    )
  },
)

// ─── Pill ───────────────────────────────────────────────────
export function Pill({ tone = 'neutral', children }: { tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'accent'; children: ReactNode }) {
  const tones: Record<string, string> = {
    neutral: 'bg-line/60 text-ink-soft',
    success: 'bg-success/10 text-success',
    warning: 'bg-warning/10 text-warning',
    danger: 'bg-danger/10 text-danger',
    accent: 'bg-accent-subtle text-accent',
  }
  return <span className={clsx('pill', tones[tone])}>{children}</span>
}

// ─── ProgressRing ───────────────────────────────────────────
export function ProgressRing({ value, total, size = 96, stroke = 8, label }: { value: number; total: number; size?: number; stroke?: number; label?: ReactNode }) {
  const r = (size - stroke) / 2
  const C = 2 * Math.PI * r
  const ratio = total > 0 ? Math.min(1, value / total) : 0
  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} stroke="rgba(0,0,0,.06)" strokeWidth={stroke} fill="none" />
        <circle
          cx={size / 2} cy={size / 2} r={r}
          stroke="currentColor"
          strokeWidth={stroke}
          fill="none"
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={C * (1 - ratio)}
          className="text-accent transition-[stroke-dashoffset] duration-500 ease-apple"
        />
      </svg>
      <div className="absolute inset-0 grid place-items-center text-center">
        <div className="text-[15px] font-semibold leading-none">
          {label ?? `${Math.round(ratio * 100)}%`}
        </div>
      </div>
    </div>
  )
}

// ─── Toast (lightweight) ────────────────────────────────────
import { create } from 'zustand'
import { useEffect } from 'react'

type ToastTone = 'success' | 'danger' | 'neutral'
interface Toast { id: number; tone: ToastTone; msg: string }
interface ToastState {
  items: Toast[]
  push: (msg: string, tone?: ToastTone) => void
  remove: (id: number) => void
}
export const useToast = create<ToastState>((set, get) => ({
  items: [],
  push: (msg, tone = 'neutral') => {
    const id = Date.now() + Math.random()
    set({ items: [...get().items, { id, msg, tone }] })
    setTimeout(() => get().remove(id), 3500)
  },
  remove: (id) => set({ items: get().items.filter(t => t.id !== id) }),
}))

export function ToastContainer() {
  const items = useToast(s => s.items)
  return (
    <div className="fixed top-4 right-4 z-50 space-y-2 pointer-events-none">
      {items.map(t => {
        const tones: Record<ToastTone, string> = {
          neutral: 'bg-ink text-white',
          success: 'bg-success text-white',
          danger: 'bg-danger text-white',
        }
        return (
          <div key={t.id} className={clsx('px-4 py-2.5 rounded-btn shadow-md text-[14px] pointer-events-auto', tones[t.tone])}>
            {t.msg}
          </div>
        )
      })}
    </div>
  )
}
