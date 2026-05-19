"""Message routing engine for Armature channel connectors."""
from __future__ import annotations
import re
from armature.channels.models import ChannelSpec


class MessageRouter:
    def __init__(self, spec: ChannelSpec) -> None:
        self._spec = spec

    def find_workflow(self, channel_name: str, text: str) -> str | None:
        """Return the first matching workflow path for text in the given channel."""
        for channel in self._spec.channels:
            if channel.name != channel_name:
                continue
            for route in channel.routes:
                if re.search(route.pattern, text):
                    return route.workflow
        return None
