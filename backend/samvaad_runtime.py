"""Secure browser-session configuration for the Samvaad conversational SDK.

The browser SDK needs a short-lived signed WebSocket URL, but the long-lived
SAMVAAD_API_KEY must never be shipped to the browser.  The frontend therefore
points the SDK's ``baseUrl`` at our authenticated proxy.  This module validates
that the requested org/workspace/app is exactly Chhotu's configured agent and
uses the server-side key to obtain the signed URL from Sarvam.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


DEFAULT_ORG_ID = "019f9945-ebf7-77f9-b60b-dc1963284e44"
DEFAULT_WORKSPACE_ID = "019f9945-ebfb-76ac-9855-2f2c5985abbb"
DEFAULT_APP_ID = "Voice-Assis-9018c9fb-e7c8"
DEFAULT_AGENT_VERSION = 5
SAMVAAD_BASE_URL = "https://apps.sarvam.ai/api/app-runtime/"


class SamvaadConfigurationError(RuntimeError):
    """The server cannot start an in-app Samvaad session."""


@dataclass(frozen=True)
class SamvaadSettings:
    api_key: str
    org_id: str
    workspace_id: str
    app_id: str
    version: int | None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.org_id and self.workspace_id and self.app_id)


def settings() -> SamvaadSettings:
    raw_version = (
        os.environ.get("SAMVAAD_AGENT_VERSION") or str(DEFAULT_AGENT_VERSION)
    ).strip()
    try:
        version = int(raw_version) if raw_version else None
    except ValueError as exc:
        raise SamvaadConfigurationError(
            "SAMVAAD_AGENT_VERSION must be a whole number."
        ) from exc
    return SamvaadSettings(
        api_key=(os.environ.get("SAMVAAD_API_KEY") or "").strip(),
        org_id=(os.environ.get("SAMVAAD_ORG_ID") or DEFAULT_ORG_ID).strip(),
        workspace_id=(
            os.environ.get("SAMVAAD_WORKSPACE_ID") or DEFAULT_WORKSPACE_ID
        ).strip(),
        app_id=(os.environ.get("SAMVAAD_APP_ID") or DEFAULT_APP_ID).strip(),
        version=version,
    )


def browser_config() -> dict:
    """Return only non-secret values needed to construct the browser SDK."""
    cfg = settings()
    result = {
        "enabled": cfg.enabled,
        "org_id": cfg.org_id,
        "workspace_id": cfg.workspace_id,
        "app_id": cfg.app_id,
        "proxy_base_url": "/api/voice/samvaad/",
    }
    if cfg.version is not None:
        result["version"] = cfg.version
    if not cfg.enabled:
        result["reason"] = "SAMVAAD_API_KEY is not configured on the server."
    return result


async def get_signed_url(
    org_id: str,
    workspace_id: str,
    app_id: str,
    *,
    interaction_type: str,
    version: int | None,
) -> dict:
    """Fetch a signed URL for only the configured Chhotu agent."""
    cfg = settings()
    if not cfg.enabled:
        raise SamvaadConfigurationError(
            "SAMVAAD_API_KEY is not configured on the server."
        )
    if (org_id, workspace_id, app_id) != (
        cfg.org_id,
        cfg.workspace_id,
        cfg.app_id,
    ):
        raise PermissionError("This Samvaad agent is not available.")
    if interaction_type != "call":
        raise ValueError("The in-app Chhotu agent only supports call sessions.")
    if cfg.version is not None and version not in (None, cfg.version):
        raise PermissionError("This Samvaad agent version is not available.")

    chosen_version = cfg.version if cfg.version is not None else version
    params: dict[str, str | int] = {"interaction_type": "call"}
    if chosen_version is not None:
        params["version"] = chosen_version
    url = (
        f"{SAMVAAD_BASE_URL}orgs/{cfg.org_id}/workspaces/"
        f"{cfg.workspace_id}/apps/{cfg.app_id}/url"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                url,
                params=params,
                headers={"X-API-Key": cfg.api_key},
            )
    except httpx.HTTPError as exc:
        raise SamvaadConfigurationError(
            "Samvaad is temporarily unreachable."
        ) from exc
    if response.status_code >= 400:
        # Do not relay Sarvam's body: upstream errors can contain implementation
        # details, while the status is enough for the UI to choose its fallback.
        raise SamvaadConfigurationError(
            f"Samvaad session could not be created ({response.status_code})."
        )
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise SamvaadConfigurationError(
            "Samvaad returned an invalid session response."
        ) from exc
    if not payload.get("url") or not payload.get("reference_id"):
        raise SamvaadConfigurationError(
            "Samvaad returned an incomplete session response."
        )
    return {"url": payload["url"], "reference_id": payload["reference_id"]}
