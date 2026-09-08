"""Bounded route probes with durable per-tenant leases and configuration fencing."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from typing import Any

from app.core.errors import ApiError
from app.core.ids import new_id
from app.domain.model_route_readiness import (
    configuration_fingerprint, project_route_readiness, safe_probe_code, timestamp, utc_timestamp,
)
from app.model_gateway.errors import ProviderError
from app.model_gateway.registry import get_provider_manifest
from app.persistence.errors import ConcurrencyConflict


def route_facts(transaction: Any, route: Any) -> tuple:
    if route is None:
        return None, None, None, None
    targets = []
    for target in [route.get("primary") or {}, *(route.get("fallbacks") or [])]:
        model = transaction.model_configurations.get(target.get("model_configuration_id"))
        connection = transaction.provider_connections.get(model.get("provider_connection_id")) if model else None
        targets.append((model, connection))
    model, connection = targets[0]
    try:
        manifest = get_provider_manifest(model.get("provider_id", "")) if model else None
    except (ProviderError, ValueError, KeyError):
        manifest = None
    return model, connection, manifest, configuration_fingerprint(route, targets)


def route_readiness(transaction: Any, route: Any, now: Any = None) -> dict:
    model, connection, manifest, fingerprint = route_facts(transaction, route)
    return project_route_readiness(route, model=model, connection=connection, manifest=manifest,
                                   fingerprint=fingerprint, now=now or transaction.database_now())


def purpose_readiness(transaction: Any, capability: str, purpose: str, now: Any = None) -> dict:
    candidates = [item for item in transaction.model_routes.list()
                  if item.get("capability") == capability and item.get("purpose") == purpose]
    route = next((item for item in candidates if item.get("enabled", True)), candidates[0] if candidates else None)
    return route_readiness(transaction, route, now)


class ModelRouteReadinessRefresher:
    """Network never runs inside a transaction; old probes cannot publish health."""

    def __init__(self, persistence: Any, probe: Any, *, concurrency: int = 3,
                 probe_timeout: float = 15, batch_timeout: float = 30, cooldown: float = 30) -> None:
        self.persistence, self.probe = persistence, probe
        self.probe_timeout, self.batch_timeout, self.cooldown = probe_timeout, batch_timeout, cooldown
        self._concurrency, self._semaphore, self._loop = concurrency, None, None

    def _snapshot(self, route_id: str, organization_id: str) -> dict:
        with self.persistence.transaction(organization_id) as transaction:
            result = route_readiness(transaction, transaction.model_routes.get(route_id))
        return {"route_id": route_id, "readiness": result}

    async def refresh(self, route_ids: list[str], organization_id: str) -> dict:
        ids = list(dict.fromkeys(route_ids))
        if len(ids) > 64:
            raise ApiError("MODEL_ROUTE_REFRESH_LIMIT", "At most 64 routes may be refreshed together.", status_code=422)
        deadline = asyncio.get_running_loop().time() + self.batch_timeout
        tasks = [asyncio.create_task(self._one(route_id, organization_id, deadline=deadline)) for route_id in ids]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return {"items": [self._snapshot(route_id, organization_id) for route_id in ids]}

    async def test(self, route_id: str, organization_id: str) -> dict:
        if self._snapshot(route_id, organization_id)["readiness"]["route_id"] is None:
            raise ApiError("MODEL_ROUTE_NOT_FOUND", "Model route does not exist.", status_code=404)
        result = await self._one(route_id, organization_id, force=True,
                                 deadline=asyncio.get_running_loop().time() + self.batch_timeout)
        if result.get("error") is not None:
            raise result["error"]
        if "response" in result:
            return result["response"]
        state = self._snapshot(route_id, organization_id)["readiness"]
        if state["ready"]:
            return {"probe_mode": "reused", "readiness": state}
        raise ApiError("MODEL_ROUTE_NOT_READY", "Model route probe is unavailable; inspect its readiness status.",
                       status_code=409, details={"route_readiness": state})

    def _claim(self, route_id: str, organization_id: str, force: bool) -> tuple:
        with self.persistence.transaction(organization_id) as transaction:
            route = transaction.model_routes.get(route_id)
            now = transaction.database_now()
            state = route_readiness(transaction, route, now)
            if state["status"] == "checking":
                return "waiting", None
            manual_mock = force and state["reason_code"] == "NON_PRODUCTION_PROVIDER"
            if state["status"] == "configuration_invalid" and not manual_mock:
                return "skip", None
            if state["ready"] and not force:
                return "skip", None
            if state["status"] == "failed" and not state["can_refresh"] and not force:
                return "skip", None
            fingerprint = route_facts(transaction, route)[3]
            lease = {"token": new_id("route_probe"), "configuration_fingerprint": fingerprint,
                     "started_at": timestamp(now), "expires_at": timestamp(now + timedelta(seconds=self.probe_timeout + 2))}
            route["health_probe"] = lease
            route["updated_at"] = timestamp(now)
            transaction.model_routes.update(route, expected_version=route["version"])
            return "claimed", (deepcopy(route), lease)

    def _complete(self, route_id: str, organization_id: str, lease: dict, error: Any) -> bool:
        for _ in range(3):
            try:
                return self._complete_once(route_id, organization_id, lease, error)
            except ConcurrencyConflict:
                continue
        return False

    def _complete_once(self, route_id: str, organization_id: str, lease: dict, error: Any) -> bool:
        with self.persistence.transaction(organization_id) as transaction:
            route = transaction.model_routes.get(route_id)
            if route is None or (route.get("health_probe") or {}).get("token") != lease["token"]:
                return False
            now = transaction.database_now()
            model, connection, manifest, fingerprint = route_facts(transaction, route)
            state = project_route_readiness(route, model=model, connection=connection, manifest=manifest,
                                            fingerprint=fingerprint, now=now)
            valid = (fingerprint == lease["configuration_fingerprint"]
                     and utc_timestamp(lease["expires_at"]) > now
                     and (state["status"] != "configuration_invalid" or state["reason_code"] == "NON_PRODUCTION_PROVIDER"))
            route.pop("health_probe", None)
            if valid:
                route["last_health"] = {
                    "status": "failed" if error else "healthy", "checked_at": timestamp(now),
                    "configuration_fingerprint": fingerprint,
                    "reason_code": safe_probe_code(error.code) if error else "ROUTE_HEALTH_FRESH",
                    "retryable": bool(error.retryable) if error else False,
                    "retry_at": timestamp(now + timedelta(seconds=self.cooldown)) if error else None,
                }
            route["updated_at"] = timestamp(now)
            transaction.model_routes.update(route, expected_version=route["version"])
            return valid

    async def _one(self, route_id: str, organization_id: str, *, deadline: float, force: bool = False) -> dict:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._loop, self._semaphore = loop, asyncio.Semaphore(self._concurrency)
        semaphore = self._semaphore
        while loop.time() < deadline:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=max(0.001, deadline - loop.time()))
            except asyncio.TimeoutError:
                return {}
            try:
                try:
                    disposition, claimed = self._claim(route_id, organization_id, force)
                except ConcurrencyConflict:
                    disposition, claimed = "waiting", None
                if disposition == "skip":
                    return {}
                if disposition == "claimed":
                    route, lease = claimed
                    return await self._probe_claim(route, lease, organization_id, deadline)
            finally:
                semaphore.release()
            # A second caller reuses the lease winner instead of forcing a new
            # paid probe after waiting. Expired leases may be claimed safely.
            force = False
            await asyncio.sleep(min(0.05, max(0, deadline - loop.time())))
        return {}

    async def _probe_claim(self, route: dict, lease: dict, organization_id: str, deadline: float) -> dict:
        task = asyncio.create_task(self.probe(deepcopy(route), organization_id))
        error, response = None, None
        cancelled = False
        try:
            remaining = min(self.probe_timeout, max(0, deadline - asyncio.get_running_loop().time()))
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done or asyncio.get_running_loop().time() >= deadline:
                error = ProviderError("provider_timeout", "Model route probe exceeded its time budget.", retryable=True)
            else:
                response = task.result()
        except asyncio.CancelledError:
            cancelled = True
            error = ProviderError("provider_probe_cancelled", "Model route probe was cancelled.", retryable=True)
        except Exception as exc:
            error = ProviderError(safe_probe_code(getattr(exc, "code", None)), "Model route probe failed.",
                                  retryable=bool(getattr(exc, "retryable", True)))
        finally:
            if not task.done():
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=0.1)
                if not done:
                    task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
            if task.done() and not task.cancelled():
                task.exception()
        committed = self._complete(route["id"], organization_id, lease, error)
        if cancelled:
            raise asyncio.CancelledError
        if not committed:
            return {"error": ProviderError("provider_route_invalid", "Model route configuration changed during its probe.", retryable=True)}
        return {"error": error} if error else {"response": response}
