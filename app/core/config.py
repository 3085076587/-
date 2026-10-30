"""应用配置：全部从环境变量 / .env 读取，禁止把密钥写进代码。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "Ad Helper Qianchuan ERP"
    DEBUG: bool = True

    # 巨量开放平台应用凭证
    OCEANENGINE_APP_ID: str = ""
    OCEANENGINE_SECRET: str = ""
    OCEANENGINE_API_BASE: str = "https://api.oceanengine.com/open_api"

    # MySQL，示例：mysql+pymysql://root:password@127.0.0.1:3306/ad_helper?charset=utf8mb4
    DATABASE_URL: str = "mysql+pymysql://root:123456@127.0.0.1:3306/ad_helper?charset=utf8mb4"

    # 启动时自动建表，方便本地联调
    AUTO_CREATE_TABLES: bool = True

    # OAuth 回调公网地址，必须与巨量后台完全一致
    OAUTH_CALLBACK_URL: str = (
        "https://chuanqian-test.free.idcfengye.com/api/oauth/callback"
    )

    # 千川授权页；留空则按 app_id 自动拼接
    OAUTH_AUTHORIZE_URL: str = ""

    # Redis：ROI 提醒倒计时（默认 DB0）
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    # Redis：子账户今天/7天/30天数据缓存（默认 DB1）
    REDIS_CACHE_URL: str = "redis://127.0.0.1:6379/1"

    # 子账户消耗并发拉取上限
    DASHBOARD_CONCURRENCY: int = 8

    # 预设区间缓存预热间隔（秒），默认半小时
    DASHBOARD_CACHE_INTERVAL_SECONDS: int = 1800

    # 系统登录（与巨量 OAuth 无关；线上务必改掉默认值）
    LOGIN_USERNAME: str = "admin"
    LOGIN_PASSWORD: str = "admin123"
    SESSION_SECRET: str = "change-me-ad-helper-session-secret"
    SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 7

    @property
    def ocean_ready(self) -> bool:
        return bool(self.OCEANENGINE_APP_ID and self.OCEANENGINE_SECRET)

    @property
    def authorize_url(self) -> str:
        if self.OAUTH_AUTHORIZE_URL.strip():
            return self.OAUTH_AUTHORIZE_URL.strip()
        if not self.OCEANENGINE_APP_ID:
            return ""
        return (
            "https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html"
            f"?app_id={self.OCEANENGINE_APP_ID}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
