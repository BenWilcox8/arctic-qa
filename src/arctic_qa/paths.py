from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import DataRootError


DEFAULT_DATA_ROOT = Path("/mnt/crdata/research-abstention")
NAMESPACE = "arctic-qa"


@dataclass(frozen=True)
class DataPaths:
    data_root: Path
    namespace: Path

    @classmethod
    def open(
        cls, value: str | Path | None, *, test_mode: bool = False, create: bool = True
    ) -> "DataPaths":
        root = Path(value).resolve() if value else DEFAULT_DATA_ROOT
        if root != DEFAULT_DATA_ROOT and not test_mode:
            raise DataRootError("A nonstandard data root requires --test-mode.")
        if not root.is_dir():
            raise DataRootError(f"The data root is missing: {root}")
        if root == DEFAULT_DATA_ROOT and not _is_mounted(root):
            raise DataRootError(f"The expected mounted drive is not mounted: {root}")
        if not os.access(root, os.W_OK | os.X_OK):
            raise DataRootError(f"The data root is not writable: {root}")
        namespace = root / NAMESPACE
        if create:
            namespace.mkdir(mode=0o700, parents=False, exist_ok=True)
            for name in (
                "originals",
                "parsed",
                "chunks",
                "manifests",
                "runs",
                "logs",
                "backups",
                "exports",
                "replay",
            ):
                (namespace / name).mkdir(mode=0o700, exist_ok=True)
        return cls(root, namespace)

    @property
    def database(self) -> Path:
        return self.namespace / "state.sqlite3"


def _is_mounted(path: Path) -> bool:
    target = str(path.resolve())
    best = ""
    try:
        with Path("/proc/self/mountinfo").open(encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 5:
                    continue
                mountpoint = fields[4].replace("\\040", " ")
                if target == mountpoint or target.startswith(
                    mountpoint.rstrip("/") + "/"
                ):
                    if len(mountpoint) > len(best):
                        best = mountpoint
    except OSError:
        return False
    return best not in ("", "/")
