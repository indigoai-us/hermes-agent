"""Tests for the Nous-HQ Runtime-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"hqr"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``hqr-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "hqr" tag namespace.

``is_nous_hqr_non_agentic`` should only match the actual Nous Research
Hermes-3 / Hermes-4 chat family.
"""

from __future__ import annotations

import pytest

from hqr_cli.model_switch import (
    _HQR_MODEL_WARNING,
    _check_hqr_model_warning,
    is_nous_hqr_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "NousResearch/HQ Runtime-3-Llama-3.1-70B",
        "NousResearch/HQ Runtime-3-Llama-3.1-405B",
        "hqr-3",
        "HQ Runtime-3",
        "hqr-4",
        "hqr-4-405b",
        "hqr_4_70b",
        "openrouter/hermes3:70b",
        "openrouter/nousresearch/hermes-4-405b",
        "NousResearch/Hqr3",
        "hqr-3.1",
    ],
)
def test_matches_real_nous_hqr_chat_models(model_name: str) -> None:
    assert is_nous_hqr_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Nous HQ Runtime 3/4"
    )
    assert _check_hqr_model_warning(model_name) == _HQR_MODEL_WARNING


