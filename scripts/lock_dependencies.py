"""Snapshot the installed runtime dependency closure; run from the project root after tests."""

import importlib.metadata as metadata
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

project = tomllib.loads(Path("pyproject.toml").read_text())
pending = [Requirement(raw).name for raw in project["project"]["dependencies"]]
seen = set()
while pending:
    name = pending.pop().lower().replace("_", "-")
    if name in seen:
        continue
    seen.add(name)
    for raw in metadata.requires(name) or []:
        requirement = Requirement(raw)
        if requirement.marker is None or requirement.marker.evaluate():
            pending.append(requirement.name)
Path("requirements.lock").write_text(
    "# Tested runtime dependency snapshot. Refresh with CI before upgrading.\n"
    + "\n".join(sorted(name + "==" + metadata.version(name) for name in seen))
    + "\n"
)
