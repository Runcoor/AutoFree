import { useEffect, useState } from 'react'
import { Trash2, Plus, KeyRound, Mail, MessageSquare, Cloud, Globe, Wallet } from 'lucide-react'
import { authApi, domainsApi, settingsApi, type CloudMailCfg, type CpaCfg, type Domain, type SmsCfg } from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, Input, Pill, useToast } from '../components/ui'

export function SettingsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-display">设置</h1>
        <p className="text-ink-soft mt-1">应用密码 · 邮件 · SMS · CPA · 域名池</p>
      </div>

      <PasswordCard />
      <CloudMailCard />
      <SmsCard />
      <CpaCard />
      <DomainsCard />
    </div>
  )
}

// ─────────────────── Password ───────────────────
function PasswordCard() {
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [busy, setBusy] = useState(false)
  const push = useToast(s => s.push)

  async function save() {
    if (!oldPw || !newPw) return push('请填完整', 'danger')
    if (newPw.length < 4) return push('新密码至少 4 位', 'danger')
    setBusy(true)
    try {
      await authApi.changePassword(oldPw, newPw)
      push('密码已更新,即将重新登录', 'success')
      setTimeout(() => { window.location.href = '/login' }, 1200)
    } catch (err: any) {
      push(err?.response?.data?.detail || '修改失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader title={<><KeyRound className="inline w-5 h-5 mr-2 -mt-0.5 text-ink-soft" />访问密码</>}
                  subtitle="修改后所有设备将被强制重新登录" />
      <CardBody className="grid md:grid-cols-2 gap-4">
        <Input label="当前密码" type="password" value={oldPw} onChange={e => setOldPw(e.target.value)} />
        <Input label="新密码" type="password" value={newPw} onChange={e => setNewPw(e.target.value)} />
        <div className="md:col-span-2">
          <Button onClick={save} loading={busy}>保存</Button>
        </div>
      </CardBody>
    </Card>
  )
}

// ─────────────────── Cloud-mail ───────────────────
function CloudMailCard() {
  const [cfg, setCfg] = useState<CloudMailCfg | null>(null)
  const [baseUrl, setBaseUrl] = useState('')
  const [pw, setPw] = useState('')
  const [busy, setBusy] = useState(false)
  const push = useToast(s => s.push)

  useEffect(() => {
    settingsApi.getCloudMail().then(c => { setCfg(c); setBaseUrl(c.base_url) })
  }, [])

  async function save() {
    setBusy(true)
    try {
      const body: any = { base_url: baseUrl }
      if (pw) body.password = pw
      const r = await settingsApi.putCloudMail(body)
      setCfg(r); setPw('')
      push('已保存', 'success')
    } catch (err: any) {
      push(err?.response?.data?.detail || '保存失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader title={<><Mail className="inline w-5 h-5 mr-2 -mt-0.5 text-ink-soft" />Cloud-Mail</>}
                  subtitle="dreamhunter2333/cloudflare_temp_email 服务地址 + 管理密码" />
      <CardBody className="grid md:grid-cols-2 gap-4">
        <Input label="服务 URL" placeholder="https://mail.example.com" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
        <Input label="管理密码" type="password"
               placeholder={cfg?.has_password ? '已设置 (留空不改)' : '未设置'}
               value={pw} onChange={e => setPw(e.target.value)} />
        <div className="md:col-span-2">
          <Button onClick={save} loading={busy}>保存</Button>
        </div>
      </CardBody>
    </Card>
  )
}

// ─────────────────── SMS ───────────────────
function SmsCard() {
  const [cfg, setCfg] = useState<SmsCfg | null>(null)
  const [provider, setProvider] = useState('5sim')
  const [apiKey, setApiKey] = useState('')
  const [service, setService] = useState('openai')
  const [country, setCountry] = useState('france')
  const [operator, setOperator] = useState('any')
  const [busy, setBusy] = useState(false)
  const [balance, setBalance] = useState<string>('—')
  const push = useToast(s => s.push)

  useEffect(() => { load() }, [])
  async function load() {
    const c = await settingsApi.getSms()
    setCfg(c)
    setProvider(c.provider); setService(c.service); setCountry(c.country); setOperator(c.operator)
  }

  async function save() {
    setBusy(true)
    try {
      const body: any = { provider, service, country, operator }
      if (apiKey) body.api_key = apiKey
      const r = await settingsApi.putSms(body); setCfg(r); setApiKey('')
      push('已保存', 'success')
    } catch (err: any) {
      push(err?.response?.data?.detail || '保存失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  async function checkBalance() {
    setBalance('查询中…')
    try {
      const r = await settingsApi.smsBalance()
      setBalance(`${r.balance} ${r.currency}`)
    } catch (err: any) {
      setBalance('—'); push(err?.response?.data?.detail || '查询失败', 'danger')
    }
  }

  return (
    <Card>
      <CardHeader title={<><MessageSquare className="inline w-5 h-5 mr-2 -mt-0.5 text-ink-soft" />SMS 接码</>}
                  subtitle="用于自动通过 OpenAI 注册的 phone gate" />
      <CardBody className="grid md:grid-cols-2 gap-4">
        <div>
          <span className="label-base">Provider</span>
          <select value={provider} onChange={e => setProvider(e.target.value)} className="input-base">
            <option value="5sim">5sim</option>
            <option value="hero-sms">hero-sms</option>
          </select>
        </div>
        <Input label="API Key" type="password"
               placeholder={cfg?.has_api_key ? '已设置 (留空不改)' : '未设置'}
               value={apiKey} onChange={e => setApiKey(e.target.value)} />
        <Input label="Service" value={service} onChange={e => setService(e.target.value)} hint="通常填 openai" />
        <Input label="Country" value={country} onChange={e => setCountry(e.target.value)} hint="如 france / uk / usa" />
        <Input label="Operator" value={operator} onChange={e => setOperator(e.target.value)} hint="any / virtual51 等" />
        <div>
          <span className="label-base flex items-center gap-1.5"><Wallet className="w-3.5 h-3.5" />当前余额</span>
          <div className="flex gap-2 items-center">
            <div className="input-base flex-1 cursor-default select-text">{balance}</div>
            <Button variant="secondary" onClick={checkBalance}>查询</Button>
          </div>
        </div>
        <div className="md:col-span-2">
          <Button onClick={save} loading={busy}>保存</Button>
        </div>
      </CardBody>
    </Card>
  )
}

// ─────────────────── CPA ───────────────────
function CpaCard() {
  const [cfg, setCfg] = useState<CpaCfg | null>(null)
  const [url, setUrl] = useState('')
  const [key, setKey] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)
  const push = useToast(s => s.push)

  useEffect(() => {
    settingsApi.getCpa().then(c => { setCfg(c); setUrl(c.url); setEnabled(c.enabled) })
  }, [])

  async function save() {
    setBusy(true)
    try {
      const body: any = { url, enabled }
      if (key) body.key = key
      const r = await settingsApi.putCpa(body); setCfg(r); setKey('')
      push('已保存', 'success')
    } catch (err: any) {
      push(err?.response?.data?.detail || '保存失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader title={<><Cloud className="inline w-5 h-5 mr-2 -mt-0.5 text-ink-soft" />CPA Push</>}
                  subtitle="注册成功后自动推送 codex auth JSON 到 CPA;不启用则只生成本地 JSON" />
      <CardBody className="grid md:grid-cols-2 gap-4">
        <Input label="CPA URL" placeholder="https://cpa.example.com" value={url} onChange={e => setUrl(e.target.value)} />
        <Input label="API Key" type="password"
               placeholder={cfg?.has_key ? '已设置 (留空不改)' : '未设置'}
               value={key} onChange={e => setKey(e.target.value)} />
        <label className="md:col-span-2 flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} className="w-4 h-4 accent-accent" />
          <span>启用自动推送</span>
        </label>
        <div className="md:col-span-2">
          <Button onClick={save} loading={busy}>保存</Button>
        </div>
      </CardBody>
    </Card>
  )
}

// ─────────────────── Domains ───────────────────
function DomainsCard() {
  const [items, setItems] = useState<Domain[]>([])
  const [adding, setAdding] = useState('')
  const push = useToast(s => s.push)

  useEffect(() => { refresh() }, [])
  function refresh() { domainsApi.list().then(setItems) }

  async function add() {
    const v = adding.trim().toLowerCase().replace(/^@/, '')
    if (!v) return
    try {
      await domainsApi.add(v); setAdding(''); refresh()
      push(`已添加 @${v}`, 'success')
    } catch (err: any) {
      push(err?.response?.data?.detail || '添加失败', 'danger')
    }
  }

  async function toggle(d: Domain) {
    await domainsApi.toggle(d.id, !d.enabled); refresh()
  }

  async function remove(d: Domain) {
    if (!confirm(`删除域名 @${d.domain}?`)) return
    await domainsApi.remove(d.id); refresh()
  }

  return (
    <Card>
      <CardHeader title={<><Globe className="inline w-5 h-5 mr-2 -mt-0.5 text-ink-soft" />域名池</>}
                  subtitle="cloud-mail 注册时可用的域名 · 启用的域名按轮询策略选用" />
      <CardBody>
        <div className="flex gap-2 mb-4">
          <input
            value={adding}
            onChange={e => setAdding(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && add()}
            placeholder="example.com"
            className="input-base flex-1"
          />
          <Button onClick={add}><Plus className="w-4 h-4" /> 添加</Button>
        </div>

        {items.length === 0
          ? <div className="py-8 text-center text-ink-muted">暂无域名 — 添加上面的一个开始</div>
          : (
            <ul className="divide-y divide-line">
              {items.map(d => (
                <li key={d.id} className="py-3 flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-mono">@{d.domain}</div>
                    <div className="text-caption text-ink-muted">
                      成 {d.success_count} · 败 {d.fail_count}
                      {d.last_used_at && ` · 最近用 ${new Date(d.last_used_at).toLocaleString('zh-CN')}`}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button onClick={() => toggle(d)}>
                      <Pill tone={d.enabled ? 'success' : 'neutral'}>
                        {d.enabled ? '已启用' : '已禁用'}
                      </Pill>
                    </button>
                    <button onClick={() => remove(d)} className="p-1.5 rounded hover:bg-danger/10 text-danger">
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
      </CardBody>
    </Card>
  )
}
