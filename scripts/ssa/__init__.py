"""smart-subagents runtime: the registry, the worker adapters, and the task
state machine.

The shell script stays the launcher and owns the task lifecycle commands.
Everything a fourth worker would otherwise fork lives here, driven by
scripts/workers.json. Python 3.9, standard library only.
"""

__all__ = ["registry", "adapters", "state"]
