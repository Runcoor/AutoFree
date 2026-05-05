import axios from 'axios'

export const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
  timeout: 30_000,
})

// 401 → 自动跳登录
api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err?.response?.status === 401) {
      const path = window.location.pathname
      if (path !== '/login') {
        window.location.href = `/login?next=${encodeURIComponent(path)}`
      }
    }
    return Promise.reject(err)
  },
)
