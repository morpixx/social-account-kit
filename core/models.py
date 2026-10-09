from dataclasses import dataclass, field


@dataclass
class MailAccount:
    email: str
    password: str = field(repr=False)


@dataclass
class Proxy:
    scheme: str
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass
class MailLetter:
    id: str
    sender: str
    subject: str
    text: str
    date: float


@dataclass
class AccountProfile:
    platform: str
    name: str
    username: str
    birth_date: str
    password: str = field(repr=False)
    created_at: str = ""
    registration_state: str = "pending"
    platform_user_id: str = ""


@dataclass
class SavedAccount:
    account_id: str
    proxy: Proxy = field(repr=False)
    cookies: list[dict] = field(repr=False)
    fingerprint: dict = field(default_factory=dict, repr=False)
    browser_state: dict = field(default_factory=dict, repr=False)
    session_storage: list[dict] = field(default_factory=list, repr=False)
    mail_account: MailAccount | None = field(default=None, repr=False)
    marketplace: dict = field(default_factory=dict, repr=False)
    profile: AccountProfile | None = field(default=None, repr=False)
    snapshot: dict = field(default_factory=dict)
