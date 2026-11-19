from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MARQUEE_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:////data/marquee.db"
    session_secret: str = "change-me"
    admin_password: str = "change-me"
    site_tz: str = "America/New_York"
    media_root: str = "/data/media"
    firmware_root: str = "/data/firmware"
    # TFTP for panel boot. Port 6969 is not a typo: the gateware sets
    # TFTP_SERVER_PORT=6969, so the BIOS asks there, not on 69.
    tftp_port: int = 6969
    tftp_enabled: bool = True
    poll_interval_s: float = 15.0
    stats_retention_days: int = 30


settings = Settings()
