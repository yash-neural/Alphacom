"""HTTP client for the platform API, acting on behalf of one user.

The MCP server is started with the *user's* token. It never uses it directly: it
swaps it (RFC 8693 token exchange) for a short-lived delegated token whose sub is the
user, act.sub is the agent, and scopes are capped by the agent's ceiling. Every API
call goes out with that token, so the API enforces the user's permissions and the
audit log records both identities.
"""

import time

import httpx


class ApiError(Exception):
    def __init__(self, status: int, problem: dict):
        super().__init__(problem.get("detail") or problem.get("title") or f"HTTP {status}")
        self.status = status
        self.problem = problem

    @property
    def slug(self) -> str:
        return str(self.problem.get("type", "")).rsplit("/", 1)[-1] or "error"


class PlatformApi:
    def __init__(self, base_url: str, user_token: str, actor: str = "support-copilot",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not user_token:
            raise ValueError("a user token is required: the copilot never runs without a user")
        self.http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=10.0)
        self.user_token = user_token
        self.actor = actor
        self._token: str | None = None
        self._token_exp = 0.0
        self.scopes: set[str] = set()

    async def _delegated_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = await self.http.post("/dev/token/exchange",
                                 json={"subject_token": self.user_token, "actor": self.actor})
        if r.status_code != 200:
            raise ApiError(r.status_code, _problem(r))
        body = r.json()
        self._token, self._token_exp = body["access_token"], time.time() + body["expires_in"]
        self.scopes = set(body["scope"].split())
        return self._token

    async def call(self, method: str, path: str, *, correlation_id: str, params: dict | None = None,
                   json: dict | None = None, headers: dict | None = None) -> httpx.Response:
        token = await self._delegated_token()
        h = {"Authorization": f"Bearer {token}", "X-Correlation-ID": correlation_id, **(headers or {})}
        r = await self.http.request(method, path, params=params, json=json, headers=h)
        if r.status_code >= 400:
            raise ApiError(r.status_code, _problem(r))
        return r

    async def aclose(self) -> None:
        await self.http.aclose()


def _problem(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {"title": f"HTTP {r.status_code}"}
