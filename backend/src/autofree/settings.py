"""应用设置 — env 加载 + 默认值。

约定:
- 业务配置(cloud-mail / SMS / CPA / 域名池)放 DB Setting 表,运行时 web 改;
- 系统级配置(数据库 URL、Session secret、密码 bootstrap)走 env。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 应用密码 ----
    app_password: str = Field(
        default="",
        description="首次启动 bootstrap 写入 User 表;之后可在设置页改密码,以 DB 为准。",
    )

    # ---- 数据库 ----
    database_url: str = Field(
        default="",
        description="SQLAlchemy URL。空则用 sqlite:///{data_dir}/autofree.db。",
    )

    # ---- Session ----
    session_secret: str = Field(
        default="",
        description="HMAC 签名 secret。空则启动时自动生成并持久化到 {data_dir}/.session_secret。",
    )
    session_cookie_name: str = "autofree_session"
    session_lifetime_days: int = 7

    # ---- 数据/输出目录 ----
    data_dir: Path = Field(default=Path("data"))

    # ---- Playwright ----
    playwright_headless: bool = True

    # ---- 其它 ----
    debug: bool = False

    # 派生
    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = (self.data_dir / "autofree.db").resolve()
        return f"sqlite:///{db_path}"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "auth").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "manual_auth").mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> AppSettings:
    s = AppSettings()
    s.ensure_dirs()
    return s
