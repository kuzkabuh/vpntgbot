# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: API роут для выдачи WireGuard конфигурации через WG-Easy
# Дата изменения: 2025-12-30
# ----------------------------------------------------------

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.wg_easy_client import WGEasyClient


router = APIRouter(tags=["vpn"])


class CreateConfigRequest(BaseModel):
    name_prefix: str = "tg_user"


@router.post("/vpn/config", response_model=dict)
async def create_vpn_config(req: CreateConfigRequest) -> dict:
    wg_url = (os.getenv("WG_EASY_URL") or "").strip()
    wg_pass = os.getenv("WG_EASY_PASSWORD") or ""

    if not wg_url:
        raise HTTPException(status_code=500, detail="WG_EASY_URL is not set")
    if not wg_pass:
        raise HTTPException(status_code=500, detail="WG_EASY_PASSWORD is not set")

    name = f"{req.name_prefix}_{int(time.time())}"

    client = WGEasyClient(base_url=wg_url, password=wg_pass)
    try:
        config_text = await client.create_and_get_configuration(name=name)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"WG-Easy error: {repr(e)}")

    return {"name": name, "config": config_text}
