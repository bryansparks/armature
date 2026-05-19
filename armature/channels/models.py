"""Spec models for Armature messaging channel connectors."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class ChannelRoute(BaseModel):
    pattern: str
    workflow: str


class ChannelConfig(BaseModel):
    name: str
    platform: Literal["telegram", "slack"]
    token: str
    signing_secret: str | None = None
    routes: list[ChannelRoute] = Field(default_factory=list)


class ChannelSpec(BaseModel):
    name: str
    channels: list[ChannelConfig] = Field(default_factory=list)
