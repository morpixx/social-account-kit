import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer
import ssl
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from playwright.async_api import Error

from core.browser import create_browser_pool
from core.crawler import create_crawler, create_proxy_configuration
from core.models import Proxy
from core.storage import AccountStorage
from crawlee import Request


PROBE_SCRIPT = """
let mouseMoves = 0;
document.addEventListener('mousemove', () => {
    document.getElementById('mouse-count').textContent = ++mouseMoves;
});
const canvas = document.createElement('canvas');
canvas.width = 200;
canvas.height = 50;
const ctx = canvas.getContext('2d');
ctx.font = '18px Arial';
ctx.fillText('Camoufox browser check', 3, 25);
const gl = document.createElement('canvas').getContext('webgl');
const debug = gl && gl.getExtension('WEBGL_debug_renderer_info');
const descriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
const probe = {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    webdriver: navigator.webdriver,
    nativeWebdriver: descriptor && descriptor.get.toString().includes('[native code]'),
    language: navigator.language,
    languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: [screen.width, screen.height],
    pixelRatio: devicePixelRatio,
    fontWidth: ctx.measureText('Camoufox browser check').width,
    hardwareConcurrency: navigator.hardwareConcurrency,
    webRTC: typeof RTCPeerConnection,
    webGL: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null,
    webGLVendor: debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : null,
    canvas: canvas.toDataURL(),
    cookie: document.cookie,
    localStorage: localStorage.getItem('test-account'),
};
document.getElementById('probe').textContent = JSON.stringify(probe);
"""


AUDIO_PROBE = """async () => {
    const context = new OfflineAudioContext(1, 44100, 44100);
    const oscillator = context.createOscillator();
    oscillator.type = 'triangle';
    oscillator.frequency.value = 10000;
    const compressor = context.createDynamicsCompressor();
    compressor.threshold.value = -50;
    compressor.knee.value = 40;
    compressor.ratio.value = 12;
    compressor.attack.value = 0;
    compressor.release.value = 0.25;
    oscillator.connect(compressor);
    compressor.connect(context.destination);
    oscillator.start();
    const buffer = await context.startRendering();
    const samples = buffer.getChannelData(0);
    let sum = 0;
    for (let index = 40000; index < samples.length; index++) sum += Math.abs(samples[index]);
    return sum;
}"""


async def read_audio_probe(page):
    # Run in the page realm: Juggler's evaluation realm blocks audio TypedArrays.
    await page.add_script_tag(content="window.audioCheck = (" + AUDIO_PROBE + ")();")
    return await page.evaluate("() => window.audioCheck")


class TestServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, label, require_auth=False, tls_context=None):
        super().__init__(("127.0.0.1", 0), TestHandler)
        self.label = label
        self.require_auth = require_auth
        self.records = []
        self.forward_port = None
        if tls_context:
            self.socket = tls_context.wrap_socket(self.socket, server_side=True)
        self.thread = Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)

    def proxy(self):
        return Proxy("http", "127.0.0.1", self.server_port, "test-user", "test:password")


class TestHandler(BaseHTTPRequestHandler):
    def check_auth(self):
        expected = "Basic " + base64.b64encode(b"test-user:test:password").decode()
        authorized = self.headers.get("Proxy-Authorization") == expected
        if self.server.require_auth and not authorized:
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="test-proxy"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        return True

    def do_GET(self):
        if not self.check_auth():
            return

        self.server.records.append({
            "path": self.path,
            "authorized": self.server.require_auth,
            "user_agent": self.headers.get("User-Agent"),
            "language": self.headers.get("Accept-Language"),
        })
        html = (
            '<html><head><title>Browser check</title><link rel="icon" href="data:,"></head>'
            f'<body><div id="label">{self.server.label}</div><div id="mouse-count">0</div><pre id="probe"></pre>'
            f'<script>{PROBE_SCRIPT}</script></body></html>'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_CONNECT(self):
        if not self.check_auth():
            return
        if self.path != f"browser-check.invalid:{self.server.forward_port}":
            self.send_error(403)
            return

        self.server.records.append({"connect": True, "authorized": True})
        with socket.create_connection(("127.0.0.1", self.server.forward_port), timeout=10) as upstream:
            self.send_response(200, "Connection established")
            self.end_headers()
            sockets = [self.connection, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 10)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.connection else self.connection
                    target.sendall(data)

    def log_message(self, format, *args):
        pass


class SocksServer(ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, origin_port):
        super().__init__(("127.0.0.1", 0), SocksHandler)
        self.origin_port = origin_port
        self.destinations = []
        self.thread = Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class SocksHandler(BaseRequestHandler):
    def read_bytes(self, count):
        data = b""
        while len(data) < count:
            part = self.request.recv(count - len(data))
            if not part:
                raise ConnectionError("Incomplete SOCKS request")
            data += part
        return data

    def handle(self):
        self.request.settimeout(10)
        version, count = self.read_bytes(2)
        methods = self.read_bytes(count)
        if version != 5 or 0 not in methods:
            return
        self.request.sendall(b"\x05\x00")
        version, command, reserved, address_type = self.read_bytes(4)
        if version != 5 or command != 1 or address_type != 3:
            return
        size = self.read_bytes(1)[0]
        host = self.read_bytes(size).decode()
        port = int.from_bytes(self.read_bytes(2), "big")
        if host != "browser-check.invalid" or port != 80:
            return
        self.server.destinations.append(host)
        with socket.create_connection(("127.0.0.1", self.server.origin_port), timeout=10) as upstream:
            self.request.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            sockets = [self.request, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 10)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(data)


@unittest.skipUnless(os.getenv("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 to launch Camoufox")
class BrowserRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.first = TestServer("first", require_auth=True)
        self.second = TestServer("second", require_auth=True)
        self.origin = TestServer("origin")

    async def asyncTearDown(self):
        self.first.stop()
        self.second.stop()
        self.origin.stop()

    async def test_fingerprints_headers_and_proxy_rotation(self):
        configuration = create_proxy_configuration([self.first.proxy(), self.second.proxy()])
        probes = []
        pool = create_browser_pool(headless=True, os="windows", locale="en-US")
        async with pool:
            for index, server in enumerate((self.first, self.second)):
                proxy_info = await configuration.new_proxy_info(
                    session_id=str(index), request=None, proxy_tier=None,
                )
                crawlee_page = await pool.new_page(proxy_info=proxy_info)
                page = crawlee_page.page
                await page.goto("http://browser-check.invalid/probe", timeout=30000)
                self.assertEqual(await page.locator("#label").inner_text(), server.label)
                probe = json.loads(await page.locator("#probe").inner_text())
                self.assertFalse(probe["webdriver"])
                self.assertTrue(probe["nativeWebdriver"])
                self.assertEqual(probe["platform"], "Win32")
                self.assertEqual(probe["language"], "en-US")
                self.assertEqual(probe["webRTC"], "undefined")
                self.assertTrue(probe["webGL"])
                self.assertEqual(probe["cookie"], "")
                self.assertIsNone(probe["localStorage"])
                self.assertEqual(probe["userAgent"], server.records[-1]["user_agent"])
                self.assertTrue(server.records[-1]["authorized"])
                await page.mouse.move(100, 100)
                await page.mouse.move(400, 250)
                mouse_moves = int(await page.locator("#mouse-count").inner_text())
                self.assertGreater(mouse_moves, 2)
                probes.append(probe)
                await page.evaluate("localStorage.setItem('test-account', 'first-account')")
                await page.evaluate("async () => { await fetch('/cache-probe'); await fetch('/cache-probe'); }")
                cache_requests = [record for record in server.records if record["path"].endswith('/cache-probe')]
                self.assertEqual(len(cache_requests), 2)
                await page.context.add_cookies([{
                    "name": "test-session", "value": "first-account", "url": "http://browser-check.invalid",
                }])
                await page.close()
                self.assertFalse(page.context.browser.is_connected())
        self.assertTrue(probes[0]["canvas"] != probes[1]["canvas"], "Canvas fingerprint repeated between browsers")
        self.assertEqual(self.origin.records, [])

    async def test_broken_proxy_does_not_fall_back_to_direct_connection(self):
        broken = Proxy("http", "127.0.0.1", self.second.server_port, "test-user", "test:password")
        self.second.stop()
        with socket.socket() as stopped_proxy:
            stopped_proxy.bind(("127.0.0.1", broken.port))
            pool = create_browser_pool(headless=True, proxy=broken)
            async with pool:
                crawlee_page = await pool.new_page()
                with self.assertRaises(Error):
                    await crawlee_page.page.goto(
                        f"http://127.0.0.1:{self.origin.server_port}/probe", timeout=10000,
                    )
                await crawlee_page.page.close()
        self.assertEqual(self.origin.records, [])

    async def test_full_crawler_uses_both_proxies(self):
        labels = []

        async def handler(context):
            labels.append(await context.page.locator("#label").inner_text())

        crawler = create_crawler(
            handler=handler,
            proxies=[self.first.proxy(), self.second.proxy()],
            headless=True,
            max_concurrency=1,
            max_request_retries=0,
        )
        await crawler.run([
            "http://browser-check.invalid/first",
            "http://browser-check.invalid/second",
        ])
        self.assertCountEqual(labels, ["first", "second"])

    async def test_saved_account_restores_browser_state_and_fingerprint(self):
        probes = {}
        with TemporaryDirectory() as directory:
            storage = AccountStorage(directory)

            async def register(context):
                account_id = context.request.unique_key
                probe = json.loads(await context.page.locator("#probe").inner_text())
                probe["audio"] = await read_audio_probe(context.page)
                self.assertEqual(probe["cookie"], "")
                self.assertIsNone(probe["localStorage"])
                probes[account_id] = probe
                await context.page.context.add_cookies([{
                    "name": "test-session", "value": account_id,
                    "url": "http://browser-check.invalid", "httpOnly": True,
                }])
                await context.page.evaluate("""async (id) => {
                    localStorage.setItem('test-account', id);
                    sessionStorage.setItem('test-session', id);
                    await new Promise((resolve, reject) => {
                        const request = indexedDB.open('account-check', 1);
                        request.onupgradeneeded = () => request.result.createObjectStore('values');
                        request.onerror = () => reject(request.error);
                        request.onsuccess = () => {
                            const database = request.result;
                            const transaction = database.transaction('values', 'readwrite');
                            transaction.objectStore('values').put(id, 'account');
                            transaction.oncomplete = () => { database.close(); resolve(); };
                            transaction.onerror = () => reject(transaction.error);
                        };
                    });
                }""", account_id)

            crawler = create_crawler(handler=register, proxies=[self.first.proxy(), self.second.proxy()],
                                     headless=True, os="windows", locale="en-US", geoip="8.8.8.8", max_concurrency=1,
                                     max_request_retries=0, account_storage=storage)
            await crawler.run([Request.from_url("http://browser-check.invalid/register", unique_key=key)
                               for key in ("account-one", "account-two")])
            self.assertEqual(set(probes), {"account-one", "account-two"})
            self.assertNotEqual(storage.load_account("account-one").fingerprint["config"],
                                storage.load_account("account-two").fingerprint["config"])
            restored = []

            async def resume(context):
                probe = json.loads(await context.page.locator("#probe").inner_text())
                probe["audio"] = await read_audio_probe(context.page)
                original = probes["account-one"]
                for field in ("userAgent", "platform", "language", "languages", "timezone", "screen",
                              "pixelRatio", "fontWidth", "hardwareConcurrency", "webGL", "webGLVendor", "canvas", "audio"):
                    self.assertEqual(probe[field], original[field], f"Restored fingerprint changed: {field}")
                self.assertEqual(probe["localStorage"], "account-one")
                self.assertEqual(await context.page.evaluate("sessionStorage.getItem('test-session')"), "account-one")
                cookies = await context.page.context.cookies()
                self.assertTrue(any(cookie["name"] == "test-session" and cookie["value"] == "account-one"
                                    and cookie["httpOnly"] for cookie in cookies))
                value = await context.page.evaluate("""() => new Promise((resolve, reject) => {
                    const request = indexedDB.open('account-check');
                    request.onerror = () => reject(request.error);
                    request.onsuccess = () => {
                        const database = request.result;
                        const read = database.transaction('values').objectStore('values').get('account');
                        read.onsuccess = () => { database.close(); resolve(read.result); };
                        read.onerror = () => reject(read.error);
                    };
                })""")
                self.assertEqual(value, "account-one")
                self.assertEqual(await context.page.locator("#label").inner_text(), "first")
                restored.append(True)

            crawler = create_crawler(handler=resume, proxies=[], headless=True, geoip=True, max_concurrency=1,
                                     max_request_retries=0, account_storage=AccountStorage(directory))
            await crawler.run([Request.from_url("http://browser-check.invalid/resume", unique_key="account-one")])
            self.assertEqual(restored, [True])
            files = list(Path(directory).glob("*.json"))
            self.assertEqual(len(files), 2)
            records = [json.loads(path.read_text(encoding="utf-8")) for path in files]
            self.assertEqual({record["account_id"] for record in records}, {"account-one", "account-two"})
            self.assertTrue(all(record["cookies"] and record["fingerprint"] and record["proxy"] for record in records))
            saved = storage.load_account("account-one")
            self.assertTrue(saved.fingerprint)
            self.assertEqual(saved.proxy, self.first.proxy())

    async def test_https_connect_with_proxy_authentication(self):
        openssl = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
        if not Path(openssl).is_file():
            self.skipTest("OpenSSL is needed for the local HTTPS test certificate")

        with TemporaryDirectory() as directory:
            certificate = Path(directory) / "test-cert.pem"
            key = Path(directory) / "test-key.pem"
            subprocess.run([
                openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(certificate), "-days", "1",
                "-subj", "/CN=browser-check.invalid",
                "-addext", "subjectAltName=DNS:browser-check.invalid",
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(certificate, key)
            server = TestServer("https", tls_context=tls)
            self.first.forward_port = server.server_port
            pool = create_browser_pool(headless=True, proxy=self.first.proxy())
            pool.plugins[0].browser_new_context_options["ignore_https_errors"] = True
            try:
                async with pool:
                    crawlee_page = await pool.new_page()
                    await crawlee_page.page.goto(
                        f"https://browser-check.invalid:{server.server_port}/probe", timeout=30000,
                    )
                    self.assertEqual(await crawlee_page.page.locator("#label").inner_text(), "https")
                    self.assertTrue(any(record.get("connect") for record in self.first.records))
                    self.assertTrue(server.records)
                    await crawlee_page.page.close()
            finally:
                server.stop()

    async def test_geoip_timezone_reaches_the_page(self):
        from camoufox.geolocation import get_geolocation

        expected_timezone = get_geolocation("8.8.8.8").timezone
        pool = create_browser_pool(headless=True, proxy=self.first.proxy(), geoip="8.8.8.8")
        async with pool:
            crawlee_page = await pool.new_page()
            page = crawlee_page.page
            await page.goto("http://browser-check.invalid/probe", timeout=30000)
            probe = json.loads(await page.locator("#probe").inner_text())
            self.assertEqual(probe["timezone"], expected_timezone)
            await page.close()

    async def test_socks5_without_authentication(self):
        socks = SocksServer(self.origin.server_port)
        proxy = Proxy("socks5", "127.0.0.1", socks.server_address[1], "", "")
        pool = create_browser_pool(headless=True, proxy=proxy)
        try:
            async with pool:
                crawlee_page = await pool.new_page()
                page = crawlee_page.page
                await page.goto("http://browser-check.invalid/probe", timeout=30000)
                self.assertEqual(await page.locator("#label").inner_text(), "origin")
                self.assertIn("browser-check.invalid", socks.destinations)
                await page.close()
        finally:
            socks.stop()


if __name__ == "__main__":
    unittest.main()
