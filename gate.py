"""Intercept messages before AstrBot enters its first pipeline stage."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps

from astrbot.api.event import AstrMessageEvent
from astrbot.core.pipeline.scheduler import PipelineScheduler


class EarlyMessageGate:
    """Own a reversible gate around the pipeline stage dispatcher."""

    def __init__(
        self, should_ignore: Callable[[AstrMessageEvent, dict], Awaitable[bool]]
    ) -> None:
        self.should_ignore = should_ignore
        self.active = False
        self._original = None
        self._wrapper = None

    def install(self) -> None:
        """Install once, before filters, media processing, and session waiters."""
        if self.active:
            return
        original = PipelineScheduler._process_stages

        @wraps(original)
        async def guarded_process(scheduler, event, from_stage=0):
            if (
                self.active
                and from_stage == 0
                and await self.should_ignore(event, scheduler.ctx.astrbot_config)
                and self.active
            ):
                event.should_call_llm(False)
                event.stop_event()
                event.trace.record(
                    "humanlike_blacklist", status="blocked", kind="access_control"
                )
                return
            await original(scheduler, event, from_stage)

        self._original = original
        self._wrapper = guarded_process
        self.active = True
        PipelineScheduler._process_stages = guarded_process

    def remove(self) -> None:
        """Remove our wrapper without overwriting another plugin's wrapper."""
        self.active = False
        if self._wrapper is not None and (
            PipelineScheduler._process_stages is self._wrapper
        ):
            PipelineScheduler._process_stages = self._original
