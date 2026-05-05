"""SMS 验证码 provider 集合 — 把不同接码平台的 API 差异收口在各自模块里。

oauth/_solve_phone_gate 只持有一个 SmsProvider 抽象,不关心是 5sim 还是 hero-sms。
入口 facade 在 freegen.sms.get_active_provider() 工厂。
"""
