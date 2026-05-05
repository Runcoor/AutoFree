import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from './store/auth'
import { LoginPage } from './pages/LoginPage'
import { Layout } from './components/Layout'
import { DashboardPage } from './pages/DashboardPage'
import { BatchPage } from './pages/BatchPage'
import { AccountsPage } from './pages/AccountsPage'
import { PendingPage } from './pages/PendingPage'
import { CpaPage } from './pages/CpaPage'
import { SettingsPage } from './pages/SettingsPage'
import { ToastContainer } from './components/ui'

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { authenticated, loading } = useAuth()
  const loc = useLocation()
  if (loading) return <FullScreenLoader />
  if (!authenticated) return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname)}`} replace />
  return <>{children}</>
}

function FullScreenLoader() {
  return (
    <div className="min-h-screen grid place-items-center text-ink-soft">
      <div className="animate-pulse">载入中…</div>
    </div>
  )
}

export default function App() {
  const { refresh } = useAuth()
  useEffect(() => { refresh() }, [refresh])

  return (
    <>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<RequireAuth><Layout /></RequireAuth>}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="batch" element={<BatchPage />} />
          <Route path="accounts" element={<AccountsPage />} />
          <Route path="pending" element={<PendingPage />} />
          <Route path="cpa" element={<CpaPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
      <ToastContainer />
    </>
  )
}
