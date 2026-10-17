#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""First-boot provisioning for the QwenPaw Maritime image.

Runs before supervisord starts the services:

1. If ``{QWENPAW_WORKING_DIR}/config.json`` is missing, run
   ``qwenpaw init --defaults --accept-security``.
2. If ``OPENAI_API_KEY`` is present, configure the built-in ``openai``
   provider (api key + ``OPENAI_BASE_URL``) and activate ``QWENPAW_MODEL``.
   The environment is the source of truth: it wins on every boot. When
   ``OPENAI_API_KEY`` is absent, the model configuration is left untouched
   (``qwenpaw init --defaults`` activates a free built-in model, so the
   agent works out of the box).

Everything is idempotent; safe to run on every container start.
Failures are logged but never abort the boot.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import traceback
from pathlib import Path

DEFAULT_MODEL = "gpt-5.4"


def _log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def _ensure_initialized(working_dir: Path) -> None:
    config_path = working_dir / "config.json"
    if config_path.is_file():
        _log(f"config found at {config_path}, skipping init")
        return
    working_dir.mkdir(parents=True, exist_ok=True)
    _log("no config.json found, running 'qwenpaw init --defaults'")
    subprocess.run(
        ["qwenpaw", "init", "--defaults", "--accept-security"],
        check=True,
    )
    _log("init complete")


def _configure_openai_from_env() -> None:
    from qwenpaw.providers.provider import ModelInfo
    from qwenpaw.providers.provider_manager import ProviderManager

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        _log("OPENAI_API_KEY not set; skipping model configuration")
        return

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    model_id = os.environ.get("QWENPAW_MODEL", DEFAULT_MODEL).strip()

    manager = ProviderManager.get_instance()
    if manager.get_provider("openai") is None:
        _log("built-in 'openai' provider not found; skipping")
        return

    ok = manager.update_provider(
        "openai",
        {"api_key": api_key, "base_url": base_url or None},
    )
    if not ok:
        _log("failed to update 'openai' provider; skipping")
        return
    _log(
        "openai provider configured "
        f"(base_url={base_url or 'default'}, model={model_id})"
    )

    provider = manager.get_provider("openai")
    if provider.get_model_info(model_id) is None:
        asyncio.run(provider.add_model(ModelInfo(id=model_id, name=model_id)))
        manager._save_provider(  # pylint: disable=protected-access
            provider,
            is_builtin="openai" in manager.builtin_providers,
        )
        _log(f"model '{model_id}' added to openai provider")

    active = manager.get_active_model()
    if (
        active is not None
        and active.provider_id == "openai"
        and active.model == model_id
    ):
        _log(f"active model already openai/{model_id}; nothing to do")
        return

    asyncio.run(manager.activate_model("openai", model_id))
    _log(f"active model set to openai/{model_id}")


def main() -> int:
    working_dir = Path(os.environ.get("QWENPAW_WORKING_DIR", "/data/working"))
    try:
        _ensure_initialized(working_dir)
    except Exception:  # noqa: BLE001
        _log("init failed:\n" + traceback.format_exc())
        return 0  # never block boot; healthz will stay 503 if fatal
    try:
        _configure_openai_from_env()
    except Exception:  # noqa: BLE001
        _log("model configuration failed:\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
