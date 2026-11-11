# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
import signal
import traceback

from .adapters.claude import ClaudeAdapter
from .adapters.codex import CodexAdapter
from .adapters.kiro import KiroAdapter
from .adapters.common import emit
from .bridge import ROOT


async def run():
    turn = json.loads((ROOT / "turn.json").read_text())
    adapter = {"claude": ClaudeAdapter, "codex": CodexAdapter, "kiro": KiroAdapter}[turn["harness"]](turn)
    task = asyncio.create_task(adapter.run())

    async def stop():
        try:
            await asyncio.wait_for(adapter.cancel(), 5)
        except Exception:
            pass
        task.cancel()

    async def watch_cancel():
        while not task.done():
            marker = ROOT / "cancel.json"
            if marker.exists() and json.loads(marker.read_text()).get("turn_key") == turn["turn_key"]:
                await stop()
                return
            await asyncio.sleep(.2)

    monitor = asyncio.create_task(watch_cancel())
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(stop()))
    try:
        await task
    except asyncio.CancelledError:
        emit("interrupted", {})
        raise SystemExit(130)
    except Exception as exc:
        # An exception cannot cross the process boundary, so carry the raiser's
        # own terminal/transient classification alongside its message.
        emit("error", {"message": str(exc) or type(exc).__name__, "traceback": traceback.format_exc(),
                       "terminal": getattr(exc, "terminal", None)})
        raise SystemExit(1)
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(run())
