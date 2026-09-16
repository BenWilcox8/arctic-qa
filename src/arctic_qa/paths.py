from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import DataRootError


# The data root and the credential directory are configuration, not constants of
# the code. The example values below are the ones of the machine that produced
# the runs of the paper, so a launcher that sets no environment variable keeps
# its behaviour. Any other machine sets the environment variables instead.
# `docs/REPRODUCTION.md` holds the full list.
DATA_ROOT_ENV = "ARCTIC_QA_DATA_ROOT"
CONFIG_DIR_ENV = "ARCTIC_QA_CONFIG_DIR"
EXAMPLE_DATA_ROOT = Path("/mnt/crdata/research-abstention")
NAMESPACE = "arctic-qa"


def configured_data_root() -> Path:
    """Return the default data root: the environment value, or the example."""
    value = os.environ.get(DATA_ROOT_ENV)
    if not value:
        return EXAMPLE_DATA_ROOT
    return Path(value).expanduser().resolve()


def configured_config_dir() -> Path:
    """Return the directory that holds the local credential files."""
    value = os.environ.get(CONFIG_DIR_ENV)
    if not value:
        return Path.home() / ".config" / NAMESPACE
    return Path(value).expanduser().resolve()


def default_credential_file() -> Path:
    """Return the default Gemini API key file."""
    return configured_config_dir() / "gemini-api-key"


DEFAULT_DATA_ROOT = configured_data_root()
# The mounted-drive rule protects the built-in example root, because that path
# names a drive that must be mounted before the pipeline writes to it. A root
# that the operator named through the environment is an explicit choice and
# carries no such rule: it must only be an existing writable directory.
DEFAULT_ROOT_MUST_BE_MOUNTED = DEFAULT_DATA_ROOT == EXAMPLE_DATA_ROOT


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
        if (
            root == DEFAULT_DATA_ROOT
            and DEFAULT_ROOT_MUST_BE_MOUNTED
            and not _is_mounted(root)
        ):
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
