import { useEffect, useState, type ReactNode } from 'react'
import {
  Trash2, Plus, KeyRound, Mail, MessageSquare, Cloud, Globe, RefreshCw, Lock, Check,
} from 'lucide-react'
import {
  authApi, domainsApi, settingsApi,
  type CloudMailCfg, type CpaCfg, type Domain, type SmsCfg,
} from '../api/endpoints'
import { Button, Card, CardBody, CardHeader, Input, Pill, Switch, useToast } from '../components/ui'

export function SettingsPage() {
  return (
    <div className="page">
      <div className="mb-7">
        <h1 className="text-[32px] font-extrabold tracking-[-0.02em] leading-[1.1] m-0">设置</h1>
        <p className="text-ink-soft text-[14px] mt-1.5">应用密码 · 邮件 · SMS · CPA · 域名池</p>
      </div>

      <PasswordCard />
      <CloudMailCard />
      <SmsCard />
      <CpaCard />
      <DomainsCard />
    </div>
  )
}

function SettingsCard({
  icon, title, subtitle, children, delay,
}: { icon: ReactNode; title: string; subtitle: string; children: ReactNode; delay?: number }) {
  return (
    <Card className="card-hover anim-in mb-5" style={delay ? { animationDelay: `${delay}ms` } : undefined}>
      <CardHeader icon={icon} title={title} subtitle={subtitle} />
      <CardBody>{children}</CardBody>
    </Card>
  )
}

// ─────────────────── Password ───────────────────
function PasswordCard() {
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [busy, setBusy] = useState(false)
  const push = useToast((s) => s.push)

  async function save() {
    if (!oldPw || !newPw) return push('请填完整', 'danger')
    if (newPw.length < 4) return push('新密码至少 4 位', 'danger')
    setBusy(true)
    try {
      await authApi.changePassword(oldPw, newPw)
      push('密码已更新 · 即将重新登录', 'success')
      setTimeout(() => { window.location.href = '/login' }, 1200)
    } catch (err: any) {
      push(err?.response?.data?.detail || '修改失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsCard
      icon={<Lock size={18} />}
      title="访问密码"
      subtitle="修改后所有设备将被强制重新登录"
    >
      <div className="grid gap-4 md:grid-cols-2 mb-3.5">
        <Input label="当前密码" type="password" value={oldPw} onChange={(e) => setOldPw(e.target.value)} placeholder="••••••••" />
        <Input label="新密码" type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} placeholder="至少 4 位" />
      </div>
      <Button variant="primary" onClick={save} loading={busy}>
        <Check className="w-3.5 h-3.5" />
        保存
      </Button>
    </SettingsCard>
  )
}

// ─────────────────── Cloud-mail ───────────────────
function CloudMailCard() {
  const [cfg, setCfg] = useState<CloudMailCfg | null>(null)
  const [baseUrl, setBaseUrl] = useState('')
  const [pw, setPw] = useState('')
  const [busy, setBusy] = useState(false)
  const push = useToast((s) => s.push)

  useEffect(() => {
    settingsApi.getCloudMail().then((c) => { setCfg(c); setBaseUrl(c.base_url) })
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
    <SettingsCard
      icon={<Mail size={18} />}
      title="Cloud-Mail"
      subtitle="dreamhunter2333/cloudflare_temp_email 服务地址 + 管理密码"
      delay={40}
    >
      <div className="grid gap-4 md:grid-cols-2 mb-3.5">
        <Input
          label="服务 URL"
          placeholder="https://mail.example.com"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
        />
        <Input
          label="管理密码"
          type="password"
          placeholder={cfg?.has_password ? '已设置 · 留空不改' : '未设置'}
          value={pw}
          onChange={(e) => setPw(e.target.value)}
        />
      </div>
      <Button variant="primary" onClick={save} loading={busy}>
        <Check className="w-3.5 h-3.5" />
        保存
      </Button>
    </SettingsCard>
  )
}

// ─────────────────── SMS ───────────────────
const SMS_PROVIDER_META: Record<string, {
  label: string
  countryHint: string
  operatorHint: string
  countryDefault: string
  operatorDefault: string
  docsUrl?: string
}> = {
  '5sim': {
    label: '5sim',
    countryHint: 'slug,如 france / indonesia / malaysia / thailand',
    operatorHint: 'any / virtual51 / orange / xl 等(具体见 5sim Statistics)',
    countryDefault: 'france',
    operatorDefault: 'any',
    docsUrl: 'https://5sim.net/products/openai',
  },
  'hero-sms': {
    label: 'hero-sms',
    countryHint: '英文名,如 england / france / usa(内部翻译为数字 ID)',
    operatorHint: 'any / 留空(hero-sms 默认任意运营商)',
    countryDefault: 'england',
    operatorDefault: 'any',
    docsUrl: 'https://hero-sms.com/cn/api',
  },
}
const SMS_PROVIDERS = Object.keys(SMS_PROVIDER_META)

function SmsCard() {
  const [cfg, setCfg] = useState<SmsCfg | null>(null)
  const [busyActive, setBusyActive] = useState(false)
  const push = useToast((s) => s.push)

  useEffect(() => { load() }, [])
  async function load() {
    const c = await settingsApi.getSms()
    setCfg(c)
  }

  async function setActive(provider: string) {
    if (cfg?.active === provider) return
    setBusyActive(true)
    try {
      const r = await settingsApi.setSmsActive(provider)
      setCfg(r)
      push(`已切换激活 provider → ${provider}`, 'success')
    } catch (err: any) {
      push(err?.response?.data?.detail || '切换失败', 'danger')
    } finally {
      setBusyActive(false)
    }
  }

  return (
    <SettingsCard
      icon={<MessageSquare size={18} />}
      title="SMS 接码"
      subtitle="多 provider 配置独立 · 每个 provider 的 country / operator 取值不同;可单独切换激活的"
      delay={80}
    >
      {cfg && (
        <div className="mb-4 flex items-center gap-2 flex-wrap">
          <span className="text-[12px] text-ink-soft mr-1">当前激活:</span>
          {SMS_PROVIDERS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setActive(p)}
              disabled={busyActive}
              className={
                'btn ' +
                (cfg.active === p ? 'btn-primary' : 'btn-ghost') +
                ' !h-[28px] !px-3 !text-[12px]'
              }
              title={cfg.active === p ? '当前激活' : `切换到 ${p}`}
            >
              {cfg.active === p && <Check className="w-3 h-3" />}
              {SMS_PROVIDER_META[p].label}
            </button>
          ))}
          <span className="text-[11px] text-ink-faint ml-2">
            注册流程会用「激活 provider」的配置打 phone gate
          </span>
        </div>
      )}

      {SMS_PROVIDERS.map((p) => (
        <SmsProviderForm
          key={p}
          provider={p}
          isActive={cfg?.active === p}
          block={cfg?.providers?.[p]}
          onSaved={(updated) => setCfg(updated)}
        />
      ))}
    </SettingsCard>
  )
}

function SmsProviderForm({
  provider, isActive, block, onSaved,
}: {
  provider: string
  isActive: boolean
  block: { api_key_masked: string; has_api_key: boolean; service: string; country: string; operator: string } | undefined
  onSaved: (cfg: SmsCfg) => void
}) {
  const meta = SMS_PROVIDER_META[provider]
  const [apiKey, setApiKey] = useState('')
  const [service, setService] = useState('openai')
  const [country, setCountry] = useState(meta.countryDefault)
  const [operator, setOperator] = useState(meta.operatorDefault)
  const [busy, setBusy] = useState(false)
  const [balance, setBalance] = useState('—')
  const [balanceBusy, setBalanceBusy] = useState(false)
  const push = useToast((s) => s.push)

  // block 变化时同步表单字段(切换 active 后 GET /settings/sms 刷新)
  useEffect(() => {
    if (block) {
      setService(block.service || 'openai')
      setCountry(block.country || meta.countryDefault)
      setOperator(block.operator || meta.operatorDefault)
    }
  }, [block, meta])

  async function save(setActiveAfter = false) {
    setBusy(true)
    try {
      const body: any = { provider, service, country, operator }
      if (apiKey) body.api_key = apiKey
      if (setActiveAfter) body.set_active = true
      const r = await settingsApi.putSms(body)
      onSaved(r)
      setApiKey('')
      push(setActiveAfter ? `已保存并激活 ${provider}` : `已保存 ${provider} 配置`, 'success')
    } catch (err: any) {
      push(err?.response?.data?.detail || '保存失败', 'danger')
    } finally {
      setBusy(false)
    }
  }

  async function checkBalance() {
    setBalanceBusy(true)
    setBalance('查询中…')
    try {
      const r = await settingsApi.smsBalance(provider)
      setBalance(`${r.balance} ${r.currency}`)
    } catch (err: any) {
      setBalance('—')
      push(err?.response?.data?.detail || '查询失败', 'danger')
    } finally {
      setBalanceBusy(false)
    }
  }

  return (
    <div
      className="rounded-[10px] border mb-4 p-4"
      style={{
        borderColor: isActive ? 'var(--brand-1)' : 'var(--line)',
        background: isActive ? 'rgba(0,114,255,0.04)' : 'transparent',
      }}
    >
      <div className="flex items-center justify-between mb-3.5 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-[14px]">{meta.label}</span>
          {isActive
            ? <Pill tone="info"><Check className="w-3 h-3" />激活中</Pill>
            : <Pill tone="muted">未激活</Pill>}
          {block?.has_api_key
            ? <Pill tone="success">已配置</Pill>
            : <Pill tone="warn">未配置 api_key</Pill>}
        </div>
        {meta.docsUrl && (
          <a
            href={meta.docsUrl}
            target="_blank"
            rel="noreferrer"
            className="text-[11px] text-ink-faint hover:text-brand-1"
          >
            API 文档 ↗
          </a>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2 mb-3.5">
        <Input
          label="API Key"
          type="password"
          placeholder={block?.has_api_key ? `已设置(${block.api_key_masked}) · 留空不改` : '未设置'}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <Input
          label="Service"
          value={service}
          onChange={(e) => setService(e.target.value)}
          hint="通常填 openai"
        />
        <Input
          label="Country"
          value={country}
          onChange={(e) => setCountry(e.target.value)}
          hint={meta.countryHint}
        />
        <Input
          label="Operator"
          value={operator}
          onChange={(e) => setOperator(e.target.value)}
          hint={meta.operatorHint}
        />
        <div className="field md:col-span-2">
          <label>当前余额</label>
          <div className="flex items-center gap-2">
            <input className="input mono bg-bg-soft" value={balance} readOnly />
            <Button onClick={checkBalance} loading={balanceBusy} disabled={!block?.has_api_key && !apiKey}>
              <RefreshCw className="w-3.5 h-3.5" />
              查询
            </Button>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <Button variant="primary" onClick={() => save(false)} loading={busy}>
          <Check className="w-3.5 h-3.5" />
          保存配置
        </Button>
        {!isActive && (
          <Button onClick={() => save(true)} loading={busy}>
            <Check className="w-3.5 h-3.5" />
            保存并设为激活
          </Button>
        )}
      </div>
    </div>
  )
}

// ─────────────────── CPA ───────────────────
function CpaCard() {
  const [cfg, setCfg] = useState<CpaCfg | null>(null)
  const [url, setUrl] = useState('')
  const [key, setKey] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)
  const push = useToast((s) => s.push)

  useEffect(() => {
    settingsApi.getCpa().then((c) => { setCfg(c); setUrl(c.url); setEnabled(c.enabled) })
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
    <SettingsCard
      icon={<Cloud size={18} />}
      title="CPA Push"
      subtitle="注册成功后自动推送 codex auth JSON 到 CPA · 不启用则只生成本地 JSON"
      delay={120}
    >
      <div className="grid gap-4 md:grid-cols-2 mb-3.5">
        <Input
          label="CPA URL"
          placeholder="https://cpa.example.com"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <Input
          label="API Key"
          type="password"
          placeholder={cfg?.has_key ? '已设置 · 留空不改' : '未设置'}
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
      </div>
      <div className="flex items-center gap-3 mb-3.5">
        <Switch on={enabled} onChange={setEnabled} ariaLabel="启用自动推送" />
        <span className="text-[13px] font-medium">启用自动推送</span>
      </div>
      <Button variant="primary" onClick={save} loading={busy}>
        <Check className="w-3.5 h-3.5" />
        保存
      </Button>
    </SettingsCard>
  )
}

// ─────────────────── Domains ───────────────────
function DomainsCard() {
  const [items, setItems] = useState<Domain[]>([])
  const [adding, setAdding] = useState('')
  const push = useToast((s) => s.push)

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
    <SettingsCard
      icon={<Globe size={18} />}
      title="域名池"
      subtitle="cloud-mail 注册时可用的域名 · 启用的域名按轮询策略选用"
      delay={160}
    >
      <div className="flex gap-2.5 mb-4">
        <input
          className="input flex-1"
          placeholder="example.com"
          value={adding}
          onChange={(e) => setAdding(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && add()}
        />
        <Button variant="primary" onClick={add} disabled={!adding.trim()}>
          <Plus className="w-3.5 h-3.5" />
          添加
        </Button>
      </div>

      {items.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon"><Globe size={22} /></div>
          暂无域名 — 添加上面的一个开始
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((d) => (
            <div
              key={d.id}
              className="flex items-center gap-3 px-3.5 py-2.5 bg-bg-soft rounded-[10px] border border-line"
            >
              <div className="w-8 h-8 rounded-[8px] grad-bg text-white grid place-items-center shrink-0">
                <Globe className="w-3.5 h-3.5" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="mono text-[14px] font-medium truncate">@{d.domain}</div>
                <div className="text-[11.5px] text-ink-faint mt-0.5 truncate">
                  成 {d.success_count} · 败 {d.fail_count}
                  {d.last_used_at && ` · 最近用 ${new Date(d.last_used_at).toLocaleString('zh-CN')}`}
                </div>
              </div>
              <Pill tone={d.enabled ? 'success' : 'muted'}>
                {d.enabled ? '启用中' : '已禁用'}
              </Pill>
              <Switch on={d.enabled} onChange={() => toggle(d)} ariaLabel="启用 / 禁用" />
              <button
                type="button"
                className="btn btn-ghost btn-icon"
                onClick={() => remove(d)}
                title="删除"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </SettingsCard>
  )
}
