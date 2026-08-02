"""Shared types for setup commands."""

from enum import Enum


class Scope(str, Enum):
    """Target scope for settings files written by setup commands."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
