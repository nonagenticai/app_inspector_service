"""Device farm listing endpoints."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from src.api.schemas import DeviceFarmInfo, FarmDevicesResponse, FarmsListResponse
from src.services.device_farm import FARM_PROVIDERS, get_device_farm

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inspect"])


@router.get(
    "/farms",
    operation_id="list_device_farms",
    summary="List registered device farm providers",
)
async def list_farms() -> FarmsListResponse:
    """List available device farm providers.

    Returns:
        FarmsListResponse containing all registered farm providers.
    """
    logger.info("Listing device farm providers")
    farms = [
        DeviceFarmInfo(
            name=name,
            available=True,
            description=f"{name} device farm provider",
        )
        for name in sorted(FARM_PROVIDERS.keys())
    ]
    logger.info("Returning %d farm providers", len(farms))
    return FarmsListResponse(farms=farms)


@router.get(
    "/farms/{provider}/devices",
    operation_id="list_farm_devices",
    summary="List available devices for a device farm provider",
    responses={
        404: {"description": "Unknown or unconfigured farm provider"},
    },
)
async def list_farm_devices(
    provider: str,
) -> FarmDevicesResponse:
    """List available devices for a device farm provider.

    Args:
        provider: Device farm provider name.

    Returns:
        FarmDevicesResponse with provider name and device list.

    Raises:
        HTTPException: If the provider is unknown or not configured.
    """
    logger.info("Listing devices for farm provider=%s", provider)
    if provider not in FARM_PROVIDERS:
        logger.warning("Unknown farm provider requested: %s", provider)
        raise HTTPException(status_code=404, detail=f"Unknown farm provider: {provider}")

    try:
        farm = get_device_farm(provider)
    except TypeError as exc:
        logger.warning("Provider not configured: %s (%s)", provider, exc)
        raise HTTPException(
            status_code=404,
            detail=f"Provider not found: {provider}",
        ) from exc
    t0 = time.monotonic()
    devices = await farm.list_devices()
    logger.info(
        "Listed %d devices from provider=%s in %.3fs",
        len(devices),
        provider,
        time.monotonic() - t0,
    )
    return FarmDevicesResponse(provider=provider, devices=devices)
