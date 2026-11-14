from __future__ import annotations

import asyncio
from typing import Any

import pytest

from helpbutton.agents.mock import MockPc, MockPhone
from helpbutton.common.agent_base import DeviceAgent
from helpbutton.server.brain import Brain
from helpbutton.server.config import Config, Contact
from helpbutton.server.family import FamilyLog
from helpbutton.server.orchestrator import Orchestrator
from helpbutton.server.registry import Registry


async def connect(registry: Registry, agent: DeviceAgent) -> None:
    """Propojí démona s registrem v paměti (bez sítě), stejným protokolem jako WebSocket."""

    async def send(msg: dict[str, Any]) -> None:
        async def run() -> None:
            res = await agent.invoke(msg["tool"], msg.get("args") or {})
            registry.on_result({"type": "result", "id": msg["id"], **res})

        asyncio.get_running_loop().create_task(run())

    hello = await agent._hello()
    await registry.attach(hello, send)


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(
        llm_provider="keywords", vlm_provider="none", data_dir=tmp_path, mdns=False, adb_watchdog=False
    )
    c.contacts = [
        Contact("Lenka", phone="+420 777 123 456", aliases=["lenka", "lence", "lenku"], dative="Lence")
    ]
    c.family.phone = "+420 602 000 111"
    return c


class Env:
    def __init__(self, cfg: Config, vlm: Any = None):
        self.cfg = cfg
        self.registry = Registry()
        self.family = FamilyLog(cfg.data_dir)
        self.events: list[dict[str, Any]] = []
        self.orch = Orchestrator(cfg, self.registry, Brain(cfg), self.family, emit=self._emit, vlm=vlm)
        self.phone = MockPhone("Telefon babičky", delay=0)
        self.pc = MockPc("Počítač", delay=0)

    async def _emit(self, msg: dict[str, Any]) -> None:
        self.events.append(msg)

    async def start(self, phone: bool = True, pc: bool = True) -> "Env":
        if phone:
            await connect(self.registry, self.phone)
        if pc:
            await connect(self.registry, self.pc)
        return self

    async def say(self, text: str):
        return await self.orch.handle(text)


@pytest.fixture
async def env(cfg) -> Env:
    return await Env(cfg).start()
