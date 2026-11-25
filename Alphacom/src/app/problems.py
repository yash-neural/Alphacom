"""RFC 9457 problem+json errors. Every error response in the API goes through here."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logs import correlation_id

PROBLEM_BASE = "https://api.example.internal/problems/"
PROBLEM_JSON = "application/problem+json"


class ApiProblem(Exception):
    def __init__(self, status: int, slug: str, title: str, detail: str | None = None,
                 headers: dict[str, str] | None = None, **extensions):
        super().__init__(detail or title)
        self.status = status
        self.slug = slug
        self.title = title
        self.detail = detail
        self.headers = headers or {}
        self.extensions = extensions

    def body(self, instance: str | None = None) -> dict:
        out = {"type": PROBLEM_BASE + self.slug, "title": self.title, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if instance:
            out["instance"] = instance
        out["correlationId"] = correlation_id.get()
        out.update(self.extensions)
        return out


def problem_response(p: ApiProblem, instance: str | None = None) -> JSONResponse:
    return JSONResponse(p.body(instance), status_code=p.status, headers=p.headers, media_type=PROBLEM_JSON)


def validation_problem(errors: list[dict]) -> ApiProblem:
    n = len(errors)
    return ApiProblem(400, "validation-error", "Request validation failed",
                      f"{n} field{'s are' if n != 1 else ' is'} invalid", errors=errors)


async def _api_problem_handler(request: Request, exc: ApiProblem):
    return problem_response(exc, request.url.path)


async def _validation_handler(request: Request, exc: RequestValidationError):
    errors = []
    for e in exc.errors():
        loc = list(e.get("loc", []))
        location = loc[0] if loc and loc[0] in ("body", "query", "path", "header") else "body"
        field = ".".join(str(x) for x in loc[1:]) or location
        errors.append({"location": location, "field": field, "message": e.get("msg", "invalid")})
    return problem_response(validation_problem(errors), request.url.path)


async def _http_handler(request: Request, exc: StarletteHTTPException):
    slug = {404: "not-found", 405: "method-not-allowed"}.get(exc.status_code, "http-error")
    title = {404: "Not found", 405: "Method not allowed"}.get(exc.status_code, "HTTP error")
    return problem_response(ApiProblem(exc.status_code, slug, title, str(exc.detail)), request.url.path)


def install(app) -> None:
    app.add_exception_handler(ApiProblem, _api_problem_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)


# Shorthands for the common cases.

def not_found(what: str) -> ApiProblem:
    return ApiProblem(404, "not-found", "Not found", f"{what} was not found")


def upstream_unavailable(system: str, detail: str) -> ApiProblem:
    return ApiProblem(503, "upstream-unavailable", "Upstream system unavailable", detail,
                      headers={"Retry-After": "30"}, upstream=system)
