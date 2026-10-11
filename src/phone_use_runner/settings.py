import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = _SOURCE_ROOT if (_SOURCE_ROOT / "pyproject.toml").exists() else Path.cwd()


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    config: Path
    runs: Path

    @classmethod
    def load(cls):
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        return cls(
            api_key=os.getenv("TYPESAFE_API_KEY", ""),
            model=os.getenv("TYPESAFE_MODEL", "jev-1.13.0"),
            config=Path(os.getenv("PHONE_USE_CONFIG", str(PROJECT_ROOT / ".state" / "bridge.json"))),
            runs=Path(os.getenv("PHONE_USE_RUNS", str(PROJECT_ROOT / "runs"))),
        )
