import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


load_dotenv()


@dataclass
class AppConfig:
    notletters_token: str = field(default="", repr=False)
    lzt_token: str = field(default="", repr=False)
    headless: bool = False
    max_concurrency: int = 3
    page_timeout: int = 45
    data_dir: str = "data"


def load_config() -> AppConfig:
    notletters_token = os.getenv("NOTLETTERS_TOKEN", "")
    lzt_token = os.getenv("LZT_TOKEN", "")

    config = AppConfig(
        notletters_token=notletters_token,
        lzt_token=lzt_token,
    )

    return config
