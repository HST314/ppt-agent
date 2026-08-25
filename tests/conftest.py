from pathlib import Path
from shutil import copy2

import pytest
import yaml

from configs.runtime import ManagedRuntime


@pytest.fixture
def mock_runtime(tmp_path: Path) -> ManagedRuntime:
    """Use an explicit free test provider; product defaults stay on real Ark."""

    root = Path(__file__).parents[1]
    runtime_root = tmp_path / "mock-runtime"
    runtime_root.mkdir()
    copy2(root / "runtime.yaml", runtime_root / "runtime.yaml")
    payload = yaml.safe_load((root / "model_config.yaml").read_text(encoding="utf-8"))
    payload["model_config_id"] = "ppt-agent-test-mock"
    for binding in payload["state_bindings"]:
        binding.update(
            provider="mock",
            model="deterministic-preview",
            parameters={},
            fallback_model=None,
            base_url=None,
            api_key_env="TEST_MODEL_API_KEY",
        )
    (runtime_root / "model_config.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (runtime_root / "skills").mkdir()
    return ManagedRuntime(runtime_root)
