import { create } from 'zustand'

type Theme = 'light' | 'dark'

interface ThemeState {
  theme: Theme
  toggle: () => void
  set: (t: Theme) => void
}

function read(): Theme {
  if (typeof window === 'undefined') return 'light'
  const v = localStorage.getItem('autofree-theme')
  return v === 'dark' ? 'dark' : 'light'
}

function apply(t: Theme) {
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-theme', t)
    try { localStorage.setItem('autofree-theme', t) } catch {}
  }
}

export const useTheme = create<ThemeState>((set, get) => ({
  theme: read(),
  toggle: () => {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark'
    apply(next)
    set({ theme: next })
  },
  set: (t) => { apply(t); set({ theme: t }) },
}))
