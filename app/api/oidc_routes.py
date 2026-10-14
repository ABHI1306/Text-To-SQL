"""OpenID Connect login routes; the session they open is the same one /auth/login issues."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.dependencies import get_oidc_service
from app.api.routes import _auth_response, _set_refresh_cookie
from app.auth.oidc import OidcLoginService
from app.auth.service import AuthenticationError
from app.config import get_settings

router = APIRouter(prefix="/api/v1", tags=["Auth"])

STATE_COOKIE = "oidc_state"
STATE_COOKIE_PATH = "/api/v1/auth/oidc"

OidcDependency = Annotated[OidcLoginService | None, Depends(get_oidc_service)]


def _require_oidc(service: OidcLoginService | None) -> OidcLoginService:
    if service is None:
        raise HTTPException(status_code=404, detail="Single sign-on is not configured")
    return service


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _secure_cookies() -> bool:
    return get_settings().app_env.lower() not in {"development", "test"}


def _clear_state_cookie(response: Response) -> None:
    response.delete_cookie(
        key=STATE_COOKIE,
        httponly=True,
        secure=_secure_cookies(),
        samesite="lax",
        path=STATE_COOKIE_PATH,
    )


@router.get("/auth/oidc/login", status_code=302)
def oidc_login(request: Request, service: OidcDependency) -> RedirectResponse:
    oidc = _require_oidc(service)
    try:
        authorization = oidc.begin(_client_key(request))
    except AuthenticationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    response = RedirectResponse(authorization.url, status_code=302)
    # Lax, not Strict: the browser comes back from the identity provider on a cross-site
    # navigation, and a Strict cookie would not be sent with it.
    response.set_cookie(
        key=STATE_COOKIE,
        value=authorization.state,
        httponly=True,
        secure=_secure_cookies(),
        samesite="lax",
        path=STATE_COOKIE_PATH,
        max_age=oidc.ttl_seconds,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/oidc/callback")
def oidc_callback(
    request: Request,
    service: OidcDependency,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    oidc = _require_oidc(service)
    try:
        if error:  # the provider refused, or the person cancelled
            raise AuthenticationError("Single sign-on failed")
        result = oidc.complete(
            code=code,
            state=state,
            browser_state=request.cookies.get(STATE_COOKIE),
            client_key=_client_key(request),
        )
    except AuthenticationError:
        failed = JSONResponse({"detail": "Single sign-on failed"}, status_code=401)
        _clear_state_cookie(failed)
        failed.headers["Cache-Control"] = "no-store"
        return failed
    response = JSONResponse(_auth_response(result))
    _clear_state_cookie(response)
    _set_refresh_cookie(response, result.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    return response
