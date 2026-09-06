import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return int(value)


def _csv_int(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(item.strip()) for item in raw.split(",") if item.strip()}


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    owner_id: int = _int("OWNER_ID")
    admin_ids: set[int] = field(default_factory=lambda: _csv_int("ADMIN_IDS"))
    database_url: str = os.getenv("DATABE_URL") or os.getenv("DATABASE_URL", "")
    database_name: str = os.getenv("DATABASE_NAME", "AutoMEMBot")
    admin_contact: str = os.getenv("ADMIN_CONTACT", "")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @property
    def all_admin_ids(self) -> set[int]:
        return {self.owner_id, *self.admin_ids} - {0}

    def validate(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.owner_id:
            missing.append("OWNER_ID")
        if not self.database_url:
            missing.append("DATABE_URL or DATABASE_URL")
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


settings = Settings()