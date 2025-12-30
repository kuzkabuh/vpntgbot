# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: Нативный HTTP-клиент WG-Easy (aiohttp): login, create client, list clients, get configuration
# Дата изменения: 2025-12-30
# ----------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp


@dataclass
class WGEasyHTTP:
    base_url: str
    password: str
    timeout: float = 15.0

    def _base(self) -> str:
        return (self.base_url or "").rstrip("/")

    async def _request_json(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        json_data: Optional[dict] = None,
        expected_status: int = 200,
    ) -> Any:
        url = f"{self._base()}{path}"
        async with session.request(method, url, json=json_data) as r:
            text = await r.text()
            if r.status != expected_status:
                raise aiohttp.ClientResponseError(
                    request_info=r.request_info,
                    history=r.history,
                    status=r.status,
                    message=text[:500],
                    headers=r.headers,
                )
            if "application/json" in (r.headers.get("Content-Type") or ""):
                return await r.json()
            return text

    async def _request_text(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        expected_status: int = 200,
    ) -> str:
        url = f"{self._base()}{path}"
        async with session.request(method, url) as r:
            text = await r.text()
            if r.status != expected_status:
                raise aiohttp.ClientResponseError(
                    request_info=r.request_info,
                    history=r.history,
                    status=r.status,
                    message=text[:500],
                    headers=r.headers,
                )
            return text

    async def login(self, session: aiohttp.ClientSession) -> None:
        data = await self._request_json(
            session,
            "POST",
            "/api/session",
            json_data={"password": self.password},
            expected_status=200,
        )
        if not isinstance(data, dict) or data.get("success") is not True:
            raise RuntimeError(f"WG-Easy login failed: {data}")

    async def list_clients(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        data = await self._request_json(
            session,
            "GET",
            "/api/wireguard/client",
            expected_status=200,
        )
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected clients list: {data}")
        return data

    async def create_client(self, session: aiohttp.ClientSession, name: str) -> None:
        data = await self._request_json(
            session,
            "POST",
            "/api/wireguard/client",
            json_data={"name": name},
            expected_status=200,
        )
        if not isinstance(data, dict) or data.get("success") is not True:
            raise RuntimeError(f"WG-Easy create_client failed: {data}")

    async def find_client_id_by_name(self, session: aiohttp.ClientSession, name: str) -> Optional[str]:
        clients = await self.list_clients(session)
        for c in clients:
            if c.get("name") == name and c.get("id"):
                return str(c["id"])
        return None

    async def get_configuration(self, session: aiohttp.ClientSession, client_id: str) -> str:
        # Важно: именно /configuration, как ты проверил curl’ом
        return await self._request_text(
            session,
            "GET",
            f"/api/wireguard/client/{client_id}/configuration",
            expected_status=200,
        )

    async def create_and_get_config(self, name: str) -> Dict[str, str]:
        if not self._base():
            raise RuntimeError("WG-EASY_URL is empty")
        if not self.password:
            raise RuntimeError("WG_EASY_PASSWORD is empty")

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await self.login(session)
            await self.create_client(session, name)
            client_id = await self.find_client_id_by_name(session, name)
            if not client_id:
                raise RuntimeError("Client created but ID not found in list")
            cfg = await self.get_configuration(session, client_id)
            return {"id": client_id, "name": name, "config": cfg}
