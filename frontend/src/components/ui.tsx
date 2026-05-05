import { forwardRef, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes, useEffect } from 'react'
import { create } from 'zustand'
import clsx from 'clsx'

// ─── Card ───────────────────────────────────────────────────
export function Card({
  children,
  className,
  hover,
  ...rest
}: { children: ReactNode; className?: string; hover?: boolean } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div {...rest} className={clsx('card', hover && 'card-hover', className)}>
      {children}
    </div>
  )
}

export function CardHeader({
  icon,
  title,
  subtitle,
  action,
  className,
}: {
  icon?: ReactNode
  title: ReactNode
  subtitle?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={clsx('card-header', className)}>
      <div className="flex items-center gap-3.5 min-w-0">
        {icon && (
          <div className="w-10 h-10 rounded-[10px] grad-bg-soft text-brand-1 grid place-items-center shrink-0">
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <h3 className="truncate">{title}</h3>
          {subtitle && <div className="sub">{subtitle}</div>}
        </div>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}

export function CardBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx('card-body', className)}>{children}</div>
}

// ─── Button ─────────────────────────────────────────────────
type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'

export const Button = forwardRef<
  HTMLButtonElement,
  { variant?: Variant; loading?: boolean; iconOnly?: boolean } & ButtonHTMLAttributes<HTMLButtonElement>
>(function Button({ variant = 'secondary', loading, iconOnly, className, children, disabled, ...rest }, ref) {
  const cls = {
    primary: 'btn btn-primary',
    secondary: 'btn',
    ghost: 'btn btn-ghost',
    danger: 'btn btn-danger',
  }[variant]
  return (
    <button
      ref={ref}
      {...rest}
      disabled={disabled || loading}
      className={clsx(cls, iconOnly && 'btn-icon', className)}
    >
      {loading && (
        <span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />
      )}
      {children}
    </button>
  )
})

// ─── Input ──────────────────────────────────────────────────
export const Input = forwardRef<
  HTMLInputElement,
  { label?: string; hint?: string; error?: string } & InputHTMLAttributes<HTMLInputElement>
>(function Input({ label, hint, error, className, ...rest }, ref) {
  return (
    <div className="field">
      {label && <label>{label}</label>}
      <input
        ref={ref}
        {...rest}
        className={clsx(
          'input',
          error && '!border-danger focus:!ring-danger/20',
          className,
        )}
      />
      {hint && !error && <span className="text-[11px] text-ink-faint">{hint}</span>}
      {error && <span className="text-[11px] text-danger">{error}</span>}
    </div>
  )
})

// ─── Select ─────────────────────────────────────────────────
export const Select = forwardRef<
  HTMLSelectElement,
  { label?: string; hint?: string } & SelectHTMLAttributes<HTMLSelectElement>
>(function Select({ label, hint, className, children, ...rest }, ref) {
  return (
    <div className="field">
      {label && <label>{label}</label>}
      <select ref={ref} {...rest} className={clsx('select', className)}>
        {children}
      </select>
      {hint && <span className="text-[11px] text-ink-faint">{hint}</span>}
    </div>
  )
})

// ─── Textarea ───────────────────────────────────────────────
export const Textarea = forwardRef<
  HTMLTextAreaElement,
  { label?: string; hint?: string } & TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ label, hint, className, ...rest }, ref) {
  return (
    <div className="field">
      {label && <label>{label}</label>}
      <textarea
        ref={ref}
        {...rest}
        className={clsx(
          'input mono',
          'h-auto py-3 leading-relaxed text-[13px] resize-y',
          className,
        )}
      />
      {hint && <span className="text-[11px] text-ink-faint">{hint}</span>}
    </div>
  )
})

// ─── Pill ───────────────────────────────────────────────────
type Tone = 'neutral' | 'success' | 'warn' | 'danger' | 'info' | 'muted'
export function Pill({ tone = 'neutral', children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  const tones: Record<Tone, string> = {
    neutral: 'pill-muted',
    muted: 'pill-muted',
    success: 'pill-success',
    warn: 'pill-warn',
    danger: 'pill-danger',
    info: 'pill-info',
  }
  return <span className={clsx('pill', tones[tone], className)}>{children}</span>
}

// ─── Switch ─────────────────────────────────────────────────
export function Switch({ on, onChange, ariaLabel }: { on: boolean; onChange: (next: boolean) => void; ariaLabel?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={ariaLabel}
      onClick={() => onChange(!on)}
      className={clsx('switch', on && 'on')}
    />
  )
}

// ─── Segmented control ──────────────────────────────────────
export function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: ReactNode }[]
}) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={value === o.value ? 'active' : ''}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

// ─── ProgressBar ────────────────────────────────────────────
export function ProgressBar({ value, total, className }: { value: number; total: number; className?: string }) {
  const pct = total > 0 ? Math.min(100, (value / total) * 100) : 0
  return (
    <div className={clsx('progress', className)}>
      <div style={{ width: `${pct}%` }} />
    </div>
  )
}

// ─── Live dot ───────────────────────────────────────────────
export function LiveDot({ tone = 'success' }: { tone?: 'success' | 'info' }) {
  return <span className={clsx('live-dot', tone === 'info' && 'info')} />
}

// ─── Animated number counter ────────────────────────────────
import { useState, useRef } from 'react'
export function Counter({ value, duration = 1200, decimals = 0, suffix = '' }: { value: number; duration?: number; decimals?: number; suffix?: string }) {
  const [v, setV] = useState(0)
  const valueRef = useRef(value)
  valueRef.current = value
  useEffect(() => {
    let raf = 0
    const start = performance.now()
    const from = 0
    const to = value
    const step = (now: number) => {
      const p = Math.min(1, (now - start) / duration)
      const eased = 1 - Math.pow(1 - p, 3)
      setV(from + (to - from) * eased)
      if (p < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [value, duration])
  return <>{v.toFixed(decimals)}{suffix}</>
}

// ─── Sparkline ──────────────────────────────────────────────
export function Sparkline({ data, w = 80, h = 28 }: { data: number[]; w?: number; h?: number }) {
  if (!data.length) return null
  const max = Math.max(...data, 1)
  const min = Math.min(...data)
  const range = max - min || 1
  const pts = data
    .map((d, i) => `${(i / Math.max(1, data.length - 1)) * w},${h - ((d - min) / range) * (h - 4) - 2}`)
    .join(' ')
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
      <defs>
        <linearGradient id="sparkGrad" x1="0" x2="1">
          <stop offset="0" stopColor="#0072ff" />
          <stop offset="1" stopColor="#00c6ff" />
        </linearGradient>
        <linearGradient id="sparkFill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="#00c6ff" stopOpacity="0.3" />
          <stop offset="1" stopColor="#00c6ff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <polyline fill="none" stroke="url(#sparkGrad)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" points={pts} />
      <polyline fill="url(#sparkFill)" stroke="none" points={`0,${h} ${pts} ${w},${h}`} />
    </svg>
  )
}

// ─── Toast (lightweight) ────────────────────────────────────
type ToastTone = 'success' | 'danger' | 'neutral'
interface Toast {
  id: number
  tone: ToastTone
  msg: string
}
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
  remove: (id) => set({ items: get().items.filter((t) => t.id !== id) }),
}))

export function ToastContainer() {
  const items = useToast((s) => s.items)
  return (
    <div className="fixed bottom-6 right-6 z-[200] flex flex-col gap-2.5 pointer-events-none">
      {items.map((t) => {
        const tones: Record<ToastTone, string> = {
          neutral: 'border-line-strong',
          success: 'border-success/40',
          danger: 'border-danger/40',
        }
        const dotTones: Record<ToastTone, string> = {
          neutral: 'bg-brand-1',
          success: 'bg-success',
          danger: 'bg-danger',
        }
        return (
          <div
            key={t.id}
            className={clsx(
              'min-w-[280px] bg-bg-elev rounded-[12px] px-4 py-3.5 border shadow-md',
              'flex items-center gap-2.5 text-[13px] font-medium pointer-events-auto',
              'animate-fade-in-up',
              tones[t.tone],
            )}
          >
            <span className={clsx('w-2 h-2 rounded-full shrink-0', dotTones[t.tone])} />
            <span className="text-ink">{t.msg}</span>
          </div>
        )
      })}
    </div>
  )
}
