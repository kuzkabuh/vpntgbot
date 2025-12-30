# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: Клиент WG-Easy API (логин, список клиентов, создание, получение конфигурации)
# Дата изменения: 2025-12-30
# ----------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class WGEasyClient:
    base_url: str
    password: str
    timeout: float = 15.0

    def _normalize_base(self) -> str:
        return self.base_url.rstrip("/")

    async def _login(self, client: httpx.AsyncClient) -> None:
        url = f"{self._normalize_base()}/api/session"
        r = await client.post(url, json={"password": self.password})
        r.raise_for_status()

        # WG-Easy возвращает {"success": true}
        data = r.json()
        if not isinstance(data, dict) or data.get("success") is not True:
            raise RuntimeError(f"WG-Easy login failed: {data}")

    async def list_clients(self) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            await self._login(client)
            url = f"{self._normalize_base()}/api/wireguard/client"
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected clients list response: {data}")
            return data

    async def create_client(self, name: str) -> None:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            await self._login(client)
            url = f"{self._normalize_base()}/api/wireguard/client"
            r = await client.post(url, json={"name": name})
            r.raise_for_status()
            data = r.json()
            # Обычно {"success": true}
            if not isinstance(data, dict) or data.get("success") is not True:
                raise RuntimeError(f"WG-Easy create client failed: {data}")

    async def get_client_id_by_name(self, name: str) -> Optional[str]:
        clients = await self.list_clients()
        for c in clients:
            if c.get("name") == name and c.get("id"):
                return str(c["id"])
        return None

    async def get_configuration(self, client_id: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            await self._login(client)
            url = f"{self._normalize_base()}/api/wireguard/client/{client_id}/configuration"
            r = await client.get(url)
            r.raise_for_status()
            # Это plain text конфиг
            return r.text

    async def create_and_get_configuration(self, name: str) -> str:
        await self.create_client(name)
        client_id = await self.get_client_id_by_name(name)
        if not client_id:
            raise RuntimeError("Created client but cannot find it in client list (no id).")
        return await self.get_configuration(client_id)
