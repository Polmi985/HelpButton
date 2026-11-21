"""Spuštění: python -m helpbutton.server [--config helpbutton.toml]"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from ..common.agent_base import setup_logging
from .app import create_app
from .config import load_config


def main() -> None:
    p = argparse.ArgumentParser(description="HelpButton – tlačítko pro seniora a chat Jiřík (jeden server)")
    p.add_argument("--config", help="cesta k helpbutton.toml")
    p.add_argument("--port", type=int)
    p.add_argument("--llm", help="porozumění větám: auto | claude | ollama | gemini | keywords")
    p.add_argument("--vlm", help="GUI agent: auto | claude | gemini | ollama | none")
    p.add_argument("--no-mdns", action="store_true")
    p.add_argument("--local", action="store_true", help="poslouchat jen na 127.0.0.1 (desktopová aplikace Jiřík)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--api-key",
        action="store_true",
        help="vypíše klíč API pro klienty z jiných počítačů (ESP32, Home Assistant) a skončí",
    )
    a = p.parse_args()
    setup_logging(a.verbose)
    cfg = load_config(a.config)
    if a.api_key:
        from ..common.protocol import api_key

        if not cfg.token:
            raise SystemExit("V helpbutton.toml není [helpbutton] token – API je otevřené, klíč není potřeba.")
        print(api_key(cfg.token))
        return
    if a.port:
        cfg.port = a.port
    if a.llm:
        cfg.llm_provider = a.llm
    if a.vlm:
        cfg.vlm_provider = a.vlm
    keys = {k: bool(os.environ.get(k)) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY")}
    logging.getLogger("helpbutton.server").info(
        "API klíče: %s", ", ".join(f"{k}={'ano' if v else 'ne'}" for k, v in keys.items())
    )
    for wanted, envs in (
        ("gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
        ("claude", ("ANTHROPIC_API_KEY",)),
    ):
        if wanted in (cfg.vlm_provider, cfg.llm_provider) and not any(keys[e] for e in envs):
            raise SystemExit(
                f"Chcete {wanted}, ale klíč {envs[0]} nevidím. Dejte ho do helpbutton.toml do [keys], "
                f'nebo v TOMTO okně: $env:{envs[0]} = "..."'
            )
    if a.no_mdns:
        cfg.mdns = False
    if a.local:
        cfg.host = "127.0.0.1"
    uvicorn.run(
        create_app(cfg),
        host=cfg.host,
        port=cfg.port,
        log_level="info" if a.verbose else "warning",
        ws_max_size=16 * 1024 * 1024,
    )


if __name__ == "__main__":
    main()
