import { create } from 'zustand'
import { authApi } from '../api/endpoints'

interface AuthState {
  authenticated: boolean
  loading: boolean
  refresh: () => Promise<void>
  login: (pw: string) => Promise<void>
  logout: () => Promise<void>
}

export const useAuth = create<AuthState>((set) => ({
  authenticated: false,
  loading: true,
  refresh: async () => {
    set({ loading: true })
    try {
      const r = await authApi.me()
      set({ authenticated: r.authenticated, loading: false })
    } catch {
      set({ authenticated: false, loading: false })
    }
  },
  login: async (pw) => {
    await authApi.login(pw)
    set({ authenticated: true })
  },
  logout: async () => {
    await authApi.logout()
    set({ authenticated: false })
  },
}))
