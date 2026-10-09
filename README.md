# social-account-kit

Browser registration, per-account session storage, and a Lolz Market API client.
The CLI registers one account at a time using Camoufox and an existing mailbox
accessible through NotLetters. Market publishing is a separate Python API.

## Requirements

- Windows and Python 3.14.
- A Camoufox browser installation and its GeoIP database.
- A NotLetters API token and a supported mailbox with its password.
- One unused proxy per new account.
- A Lolz Market API token if you want to publish accounts.

Registration adapters are covered by local browser fixtures. Live end-to-end
registration and Market publishing have not been verified. Website changes can
require updates to form selectors or authenticated-user adapters.

## Quick start

After cloning or extracting the project, open PowerShell in its parent directory:

```powershell
Set-Location .\social-account-kit
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m camoufox fetch

Copy-Item .env.example .env
Copy-Item data\proxies.example.txt data\proxies.txt
```

Replace the empty tokens in `.env` and the sample proxy in `data/proxies.txt`,
then run:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B main.py tiktok --account-id tiktok-001 --manual-checks
```

The program prompts for the mailbox address and password. Password input is
hidden. Mailboxes are not purchased automatically during registration.

All commands use the virtual environment directly; activation is optional.
Run commands from the project root so relative paths resolve correctly.

## Installation paths

| Location | Purpose |
| --- | --- |
| `.venv/` | Local Python environment; created during installation |
| `.env` | Local API tokens |
| `data/proxies.txt` | Proxy credentials for new accounts |
| `data/accounts/` | Per-account JSON snapshots and resource reservations |
| Camoufox package cache | Downloaded browser builds, outside this repository |

Camoufox manages its own browser cache. To find the selected executable:

```powershell
.\.venv\Scripts\python.exe -c "from camoufox.pkgman import launch_path; print(launch_path())"
```

GeoIP data may be downloaded on first use. Retain the browser build used to
create saved accounts: restoration checks the Camoufox package version and
browser executable hash.

See [Python for Windows](https://www.python.org/downloads/windows/) and
[Camoufox installation](https://camoufox.com/python/installation/) for setup.

## Configuration

```dotenv
NOTLETTERS_TOKEN=
LZT_TOKEN=
```

`NOTLETTERS_TOKEN` is required by the registration CLI. `LZT_TOKEN` is used by
the Market client. Environment variables take precedence over `.env` values.

### Proxies

One proxy per line:

```text
proxy.example.com:8080:username:password
http://proxy.example.com:8080:username:password
https://proxy.example.com:8443:username:password
socks5://proxy.example.com:1080::
```

Blank lines and lines starting with `#` are ignored. A password may contain
colons. IPv6 literals and SOCKS5 username/password authentication are not
supported. Duplicate proxy entries are rejected.

A proxy is reserved for one account and is never assigned to another account
by this storage. Saved accounts reuse their original proxy, even when it is no
longer in the input file. Different credentials do not prove different exit IPs.
Use a provider with a stable IP or the required region if location must remain
consistent; rotating proxy credentials alone do not preserve location.

## Registration

```powershell
# TikTok
.\.venv\Scripts\python.exe -X utf8 -B main.py tiktok --account-id tiktok-001 --manual-checks

# Instagram with an explicit mailbox address
.\.venv\Scripts\python.exe -X utf8 -B main.py instagram --account-id instagram-001 --email mailbox@example.com --manual-checks

# Headless mode with a custom proxy file
.\.venv\Scripts\python.exe -X utf8 -B main.py tiktok --account-id tiktok-002 --headless --proxies data\proxies.txt

# List CLI options
.\.venv\Scripts\python.exe -B main.py --help
```

| Argument | Purpose | Default |
| --- | --- | --- |
| `tiktok` / `instagram` | Target platform | Required |
| `--account-id ID` | Stable local identifier; use a separate ID for each account | Required |
| `--email ADDRESS` | Mailbox address; the password is prompted separately | Interactive prompt |
| `--proxies PATH` | Proxy file for new accounts | `data/proxies.txt` |
| `--headless` | Run without a visible browser window | Off |
| `--manual-checks` | Allow up to 120 seconds for a website check in the browser | Off |

`--headless` and `--manual-checks` cannot be combined. Without manual handling,
a detected website challenge stops the scenario. There is no automatic
challenge solver or guarantee that the platform will not request a check.

Name, username, password, and date of birth are generated once and saved before
form submission. Age is between 18 and 75 inclusive at registration. Instagram
receives the full name; TikTok currently stores it as local metadata.

After an explicit username rejection, the client tries a valid platform
suggestion or a new generated username, with at most four submissions. It does
not retry an ambiguous response or treat an unchanged old error as a new
rejection. Invalid or expired email codes can be replaced by a code from a
different message, with up to three attempts within a 120-second deadline.
The complete registration scenario has a 300-second deadline.

Registration is confirmed using the authenticated user's username and stable
platform ID together with session cookies. A profile link alone is insufficient.
Re-running a completed account ID returns its saved result without another
registration.

## Account files and recovery

Each account is saved as `data/accounts/<sha256(account_id)>.json`. The file
contains ordinary browser cookies plus the associated metadata:

| Field | Contents |
| --- | --- |
| `version`, `account_id` | File format and local account identity |
| `cookies` | Browser cookie array, including expiry and security attributes |
| `proxy` | Scheme, host, port, username, and password |
| `fingerprint` | Camoufox configuration, Firefox preferences, package version, and browser hash |
| `browser_state` | Cookies, local storage, and IndexedDB |
| `session_storage` | Session storage captured from the active page |
| `mail_account` | Mailbox address and password |
| `profile` | Platform, name, username, birth date, password, creation time, registration state, and platform user ID |
| `marketplace` | Listing ID, settings, and publication state |
| `snapshot` | Capture time, completeness, and any export limitation |

Files are plaintext and contain credentials. Keep them private. `.env`, working
proxy files, and account data are excluded from Git. Legacy Windows DPAPI files
are migrated to JSON after a successful write.

Snapshots are replaced atomically. The `.reservations` file preserves resource
ownership independently of individual snapshots; back it up with the account
files. A missing or damaged snapshot must be restored, not replaced by a new
registration with the same ID. Known damaged accounts do not block healthy
accounts. Missing reservation data and an unknown damaged file require manual
reconciliation.

After a submission timeout or cancellation, the state becomes `unknown` or
`needs_manual`, and the available session is saved. Automatic re-registration
is blocked. To verify the existing session, use `create_crawler` with the same
account ID and storage, and call `confirm_saved_account(context, storage)` from
`core.registration` in its handler. Profiles saved without a verified platform
ID also require this confirmation before sale.

If you have independently established that no account was created, call
`storage.reset_registration(account_id, confirmed_not_created=True)`. This
method does not contact the platform and refuses authenticated sessions.

Cookie expiry is preserved as issued by the server; saving or editing a file
does not extend server authorization. If optional browser state exceeds the
8 MiB snapshot limit or cannot be exported, essential account data is saved
with `snapshot.complete=false`. Oversized essential data is rejected while the
previous file remains intact.

HTTP cache, OPFS, Service Worker state, and session storage from closed pages
are not exported. Concurrent browsers for the same account are unsupported.
The saved fingerprint is a Camoufox configuration, not a universal profile for
Chrome or other browsers.

## Lolz Market

Publishing is separate from registration. Save this example as `sell.py`, adjust
the account ID and listing details, and run it from the project directory:

```python
import asyncio

from core.config import load_config
from core.storage import AccountStorage
from marketplaces.lzt import LztMarketClient


async def main():
    config = load_config()
    storage = AccountStorage("data/accounts")
    async with LztMarketClient(config.lzt_token) as market:
        item = await market.publish_saved_account(
            storage,
            "tiktok-001",
            title="TikTok account",
            price=10,
            category_id=20,
            currency="rub",
            information="Account setup instructions",
        )
        print(item["item_id"])


if __name__ == "__main__":
    asyncio.run(main())
```

```powershell
.\.venv\Scripts\python.exe -X utf8 -B sell.py
```

This example creates and publishes a listing. Use category `20` for TikTok or
`10` for Instagram. Saved confirmed profiles supply the account login and
password; saved mailbox credentials are supplied separately. Supported email
types are `autoreg` and `native`.

Cookies are sent as JSON in `extra.cookies`, and the saved HTTP proxy is sent
in `extra.proxy`. For new listings, proxy connection details are also appended
as JSON to the buyer-only `information` field. Your private note is preserved;
the public description is unchanged. Fingerprint configuration and other
browser storage are not uploaded. The checker accepts HTTP proxies; HTTPS and
SOCKS5 proxies are rejected rather than substituted.

Existing listings retain their saved settings and are not automatically
edited. Add buyer proxy details to older listings separately. Changed listing
settings are rejected to prevent accidental duplicate publication.

Listing creation and checking have separate persisted states. An ambiguous
creation blocks another creation. An ambiguous check requires a read of the
existing listing and reconciliation before another check. There are no
automatic POST retries. Do not delete local listing state to retry a sale.

`LztMarketError` exposes `status_code`, `retry_after`, and `result_unknown`.
Its `data` field can contain sensitive provider data; avoid logging it wholesale.
TLS verification is enabled, redirects and environment proxies are disabled.
The optional `LztMarketClient(proxy=...)` routes API traffic and is separate from
the account proxy used by the checker.

API reference: [add account](https://lzt-market.readme.io/reference/publishingadd)
and [check account](https://lzt-market.readme.io/reference/publishingcheck).

## Tests

Run the default suite without live platform registration or Market sales:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v
```

Browser tests are opt-in and use local forms and proxy servers:

```powershell
$env:RUN_BROWSER_TESTS = "1"
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v
Remove-Item Env:RUN_BROWSER_TESTS
```

They require the installed Camoufox browser and GeoIP data. The local HTTPS
proxy test also requires OpenSSL, either on `PATH` or supplied by Git for
Windows. Synthetic responses verify API contracts; they do not prove live
provider compatibility or proxy reputation.

## Project layout

```text
core/          Browser integration, configuration, mail parsing, and storage
mail/          NotLetters API client
marketplaces/  Lolz Market API client
platforms/     TikTok and Instagram registration adapters
tests/         Unit tests and local browser fixtures
data/          Proxy example and private local working files
main.py        Registration CLI
```

## Troubleshooting

| Message or symptom | Action |
| --- | --- |
| Missing or invalid `NOTLETTERS_TOKEN` | Set the token in `.env` or the environment |
| No unused proxies remain | Add another proxy for the new account; existing reservations are retained |
| Original Camoufox version required | Restore the package version and browser build used for that account |
| Missing account snapshot | Restore the account file and its reservation backup |
| Previous registration must be reconciled | Verify the saved session before any new submission |
| Previous Market result is unresolved | Inspect the existing listing and reconcile its saved state |
| Website field or identity data unavailable | Inspect the platform adapter against the current website |

On affected Windows Camoufox builds, the launcher repairs the `mozglue` manifest
dependency in a separate executable copy beside the installed browser. The
official executable is preserved; write access to that cache directory is
required.
