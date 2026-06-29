"""Agent Teams — P2P Multi-Agent Framework with Real-Time Monitoring.

Usage:
    agent-teams up                 # after `pip install` (recommended)
    python -m teams_server          # equivalent module entry point

Hermes is resolved automatically (pip `hermes-agent`, else HERMES_AGENT_PATH,
else ~/.hermes/hermes-agent). See config.ensure_hermes_importable.
"""

# Back-compat: the environment-variable prefix was renamed ``SWARM_*`` → ``TEAMS_*``
# when the project became Agent Teams. Existing deployments still set the old
# ``SWARM_*`` names (SWARM_API_KEY, SWARM_DATA_DIR, SWARM_HOST, …) in their .env /
# docker-compose; honor them by aliasing each into its ``TEAMS_*`` equivalent unless
# the new name is already set (the new name always wins). This runs on package
# import — before any submodule (config, cli, update_check, tools) reads os.environ —
# so the rest of the code only ever needs to look up the ``TEAMS_*`` name.
import os as _os

for _k, _v in list(_os.environ.items()):
    if _k.startswith("SWARM_"):
        _os.environ.setdefault("TEAMS_" + _k[len("SWARM_"):], _v)

# Single source of truth is pyproject.toml; read it back from the installed
# package metadata so the version never drifts between the two.
from importlib.metadata import version as _version, PackageNotFoundError as _PNF

try:
    __version__ = _version("agent-teams")
except _PNF:  # running from a source tree without an install
    __version__ = "0.0.0+source"
