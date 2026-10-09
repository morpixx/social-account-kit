from pathlib import Path
from core.models import Proxy


def validate_proxy(proxy: Proxy) -> None:
    if proxy.scheme not in ("http", "https", "socks5"):
        raise ValueError("Invalid proxy scheme: expected http, https or socks5")

    if not proxy.host or any(ord(char) < 33 or ord(char) == 127 or char.isspace() or char in "/\\@?#:[]%" for char in proxy.host):
        raise ValueError("Invalid proxy host")

    if not isinstance(proxy.port, int) or isinstance(proxy.port, bool):
        raise ValueError("Invalid proxy port: expected an integer")

    if not 1 <= proxy.port <= 65535:
        raise ValueError("Invalid proxy port: must be between 1 and 65535")

    if proxy.scheme == "socks5" and (proxy.username or proxy.password):
        raise ValueError("Playwright does not support SOCKS5 username/password authentication")


def parse_proxy(line: str) -> Proxy:
    clean_line = line.strip()

    if "://" in clean_line:
        scheme, clean_line = clean_line.split("://", 1)
    else:
        scheme = "http"

    parts = clean_line.split(":", 3)

    if len(parts) != 4:
        raise ValueError(
            "Invalid proxy format: expected host:port:username:password"
        )

    host, port, username, password = parts

    try:
        port = int(port)
    except ValueError:
        raise ValueError("Invalid proxy port: expected an integer") from None

    proxy = Proxy(
        scheme=scheme.lower(),
        host=host,
        port=port,
        username=username,
        password=password,
    )

    validate_proxy(proxy)

    return proxy


def load_proxies(path: Path) -> list[Proxy]:
    proxies = []

    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            clean_line = line.strip()

            if not clean_line:
                continue

            if clean_line.startswith("#"):
                continue

            try:
                proxy = parse_proxy(clean_line)
                proxies.append(proxy)

            except ValueError as error:
                print(
                    f"Invalid proxy at line {line_number}: {error}"
                )
                continue

    if not proxies:
        raise ValueError("Proxy file contains no valid proxies")

    return proxies
