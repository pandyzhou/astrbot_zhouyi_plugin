from __future__ import annotations

import asyncio
import json
import re
import ssl
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

PLUGIN_NAME = "astrbot_zhouyi_plugin"
PUBLIC_API_PREFIX = f"/api/plug/{PLUGIN_NAME}/page"
UPSTREAM_API_PREFIX = f"/api/v1/plugins/extensions/{PLUGIN_NAME}/page"
DEFAULT_CONFIG_PATH = Path("/data/astrbot/data/cmd_config.json")
DEFAULT_PAGE_ROOT = Path(__file__).resolve().parent / "pages" / "zhouyi-dashboard"
DEFAULT_PUBLIC_ORIGIN = "https://astr.zhouyihub.com:35020"
MAX_REQUEST_BODY = 64 * 1024

_ALLOWED_API_ROUTES = (
    ("GET", "/v1/bootstrap"),
    ("GET", "/v1/mc/servers"),
    ("POST", "/v1/mc/servers/add"),
    ("POST", "/v1/mc/servers/update"),
    ("POST", "/v1/mc/servers/delete"),
    ("POST", "/v1/mc/status"),
    ("GET", "/v1/mc/settings"),
    ("POST", "/v1/mc/settings/preview"),
    ("POST", "/v1/mc/settings"),
    ("GET", "/v1/mc/trends"),
    ("GET", "/v1/mc/cleanup"),
    ("POST", "/v1/mc/cleanup"),
)
_HASHED_ASSET_RE = re.compile(r"-[A-Za-z0-9_]+(?:\.[A-Za-z0-9]+)$")
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_SESSION_KEY: web.AppKey[ClientSession] = web.AppKey(
    "standalone_upstream_session", ClientSession
)
_SSL_CONTEXT_FROM_CONFIG = object()


def _problem(status: int, message: str, code: str) -> web.Response:
    return web.json_response(
        {"status": "error", "message": message, "data": {"code": code}},
        status=status,
    )


@web.middleware
async def _security_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    raw_path = request.raw_path.split("?", 1)[0]
    decoded_path = raw_path
    for _ in range(2):
        decoded_path = unquote(decoded_path)
    if ".." in decoded_path.replace("\\", "/").split("/"):
        response: web.StreamResponse = _problem(
            404, "请求路径不存在", "NOT_FOUND"
        )
    else:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            for name, value in _SECURITY_HEADERS.items():
                exc.headers[name] = value
            raise
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


class StandaloneWebService:
    """为 Zhouyi Dashboard 提供独立 HTTPS 服务，仅转发 MC API。"""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 35020,
        page_root: str | Path | None = None,
        upstream_base_url: str | None = None,
        public_origin: str = DEFAULT_PUBLIC_ORIGIN,
        ssl_context: ssl.SSLContext | None | object = _SSL_CONTEXT_FROM_CONFIG,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.page_root = Path(page_root or DEFAULT_PAGE_ROOT).expanduser()
        self.public_origin = public_origin.rstrip("/")
        self.config_path = Path(config_path).expanduser()
        self._upstream_base_url_override = (
            upstream_base_url.rstrip("/") if upstream_base_url else None
        )
        self._ssl_context_override = ssl_context
        self._config: dict[str, Any] | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None
        self._stop_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._shutdown_requested = False

    def _load_config(self) -> dict[str, Any]:
        if self._config is None:
            with self.config_path.open("r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("AstrBot 配置根节点必须是对象")
            self._config = loaded
        return self._config

    def _dashboard_config(self) -> dict[str, Any]:
        dashboard = self._load_config().get("dashboard", {})
        if not isinstance(dashboard, dict):
            raise ValueError("AstrBot dashboard 配置必须是对象")
        return dashboard

    def _upstream_base_url(self) -> str:
        if self._upstream_base_url_override:
            return self._upstream_base_url_override
        dashboard = self._dashboard_config()
        port = int(dashboard.get("port", 6185))
        ssl_config = dashboard.get("ssl", {})
        use_ssl = isinstance(ssl_config, dict) and bool(ssl_config.get("enable"))
        scheme = "https" if use_ssl else "http"
        return f"{scheme}://127.0.0.1:{port}"

    def _server_ssl_context(self) -> ssl.SSLContext | None:
        if self._ssl_context_override is not _SSL_CONTEXT_FROM_CONFIG:
            if self._ssl_context_override is not None and not isinstance(
                self._ssl_context_override, ssl.SSLContext
            ):
                raise TypeError("ssl_context 必须是 SSLContext 或 None")
            return self._ssl_context_override
        ssl_config = self._dashboard_config().get("ssl", {})
        if not isinstance(ssl_config, dict):
            raise ValueError("AstrBot dashboard.ssl 配置必须是对象")
        cert_file = ssl_config.get("cert_file")
        key_file = ssl_config.get("key_file")
        if not cert_file or not key_file:
            raise ValueError("AstrBot dashboard SSL 证书或私钥未配置")
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(str(cert_file), str(key_file))
        return context

    def _resolved_page_root(self) -> Path:
        root = self.page_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("独立页面静态根目录不存在")
        return root

    def _safe_static_file(self, relative_path: str) -> Path | None:
        try:
            root = self._resolved_page_root()
            pure_path = PurePosixPath(relative_path)
            if (
                not relative_path
                or pure_path.is_absolute()
                or any(part in {"", ".", ".."} for part in pure_path.parts)
            ):
                return None
            candidate = root.joinpath(*pure_path.parts)
            current = candidate
            while current != root:
                if current.is_symlink():
                    return None
                current = current.parent
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                return None
            return resolved
        except (OSError, RuntimeError, ValueError):
            return None

    async def _session_context(self, app: web.Application):
        timeout = ClientTimeout(total=300, connect=3)
        async with ClientSession(timeout=timeout) as session:
            app[_SESSION_KEY] = session
            yield

    def create_app(self) -> web.Application:
        app = web.Application(
            middlewares=[_security_middleware], client_max_size=MAX_REQUEST_BODY
        )
        app.cleanup_ctx.append(self._session_context)
        app.router.add_get("/", self._serve_index)
        app.router.add_get("/index.html", self._serve_index)
        app.router.add_get("/assets/{asset_path:.*}", self._serve_asset)
        for method, suffix in _ALLOWED_API_ROUTES:
            app.router.add_route(
                method,
                f"{PUBLIC_API_PREFIX}{suffix}",
                self._proxy_api,
            )
        return app

    async def _serve_index(self, request: web.Request) -> web.StreamResponse:
        path = self._safe_static_file("index.html")
        if path is None:
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _serve_asset(self, request: web.Request) -> web.StreamResponse:
        asset_path = request.match_info["asset_path"]
        path = self._safe_static_file(f"assets/{asset_path}")
        if path is None:
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        if _HASHED_ASSET_RE.search(path.name):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    def _validate_proxy_request(self, request: web.Request) -> web.Response | None:
        if "astrbot_dashboard_jwt" not in request.cookies:
            return _problem(401, "需要登录 AstrBot Dashboard", "AUTH_REQUIRED")
        if request.method != "POST":
            return None
        if request.headers.get("Origin") != self.public_origin:
            return _problem(403, "禁止跨源提交", "ORIGIN_FORBIDDEN")
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site != "same-origin":
            return _problem(403, "禁止跨站提交", "FETCH_SITE_FORBIDDEN")
        if request.content_type.lower() != "application/json":
            return _problem(
                415, "POST 请求必须使用 application/json", "INVALID_CONTENT_TYPE"
            )
        if request.content_length is not None and request.content_length > MAX_REQUEST_BODY:
            return _problem(413, "请求体超过 64KiB", "REQUEST_TOO_LARGE")
        return None

    async def _proxy_api(self, request: web.Request) -> web.StreamResponse:
        rejected = self._validate_proxy_request(request)
        if rejected is not None:
            return rejected

        body: bytes | None = None
        if request.method == "POST":
            try:
                body = await request.read()
            except web.HTTPRequestEntityTooLarge:
                return _problem(413, "请求体超过 64KiB", "REQUEST_TOO_LARGE")
            if len(body) > MAX_REQUEST_BODY:
                return _problem(413, "请求体超过 64KiB", "REQUEST_TOO_LARGE")

        suffix = request.path[len(PUBLIC_API_PREFIX) :]
        target = f"{self._upstream_base_url()}{UPSTREAM_API_PREFIX}{suffix}"
        if request.query_string:
            target = f"{target}?{request.query_string}"

        headers = {"Cookie": request.headers["Cookie"]}
        accept = request.headers.get("Accept")
        if accept:
            headers["Accept"] = accept
        if request.method == "POST":
            headers["Content-Type"] = request.headers["Content-Type"]

        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "data": body,
            "allow_redirects": False,
        }
        parsed_target = urlsplit(target)
        if parsed_target.scheme == "https" and parsed_target.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            request_kwargs["ssl"] = False

        try:
            session = request.app[_SESSION_KEY]
            async with session.request(
                request.method, target, **request_kwargs
            ) as upstream:
                response_body = await upstream.read()
                if upstream.status == 401:
                    return _problem(
                        401, "需要登录 AstrBot Dashboard", "AUTH_REQUIRED"
                    )
                response_headers: dict[str, str] = {}
                content_type = upstream.headers.get("Content-Type")
                if content_type:
                    response_headers["Content-Type"] = content_type
                return web.Response(
                    body=response_body,
                    status=upstream.status,
                    headers=response_headers,
                )
        except asyncio.TimeoutError:
            return _problem(502, "AstrBot Dashboard 响应超时", "UPSTREAM_TIMEOUT")
        except ClientError:
            return _problem(
                502, "AstrBot Dashboard 当前不可用", "UPSTREAM_UNAVAILABLE"
            )

    async def run(self) -> None:
        async with self._lifecycle_lock:
            if self._shutdown_requested:
                return
            if self._runner is None:
                runner = web.AppRunner(self.create_app(), access_log=None)
                try:
                    await runner.setup()
                    site = web.TCPSite(
                        runner,
                        self.host,
                        self.port,
                        ssl_context=self._server_ssl_context(),
                    )
                    await site.start()
                except BaseException:
                    await runner.cleanup()
                    raise
                self._runner = runner
                self._site = site
            stop_event = self._stop_event

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._shutdown_requested = True
            self._stop_event.set()
            runner = self._runner
            self._runner = None
            self._site = None
        if runner is not None:
            await runner.cleanup()
