"""The model runtime's bounds: threads, providers, and what warms up.

Groundstation REQ-027 is a requirement about a default nobody sees. ONNX Runtime
sizes its intra-operator pool against the host unless told otherwise, so a
service that never sets the number is a service that takes every core on the
machine it happens to land on — and it does that silently, correctly, and at the
expense of everything else running there. These tests are what say the number is
set, that it is the configured one, and that it survives being read out of the
environment.

The provider tests are the other half of the same idea. The list is
configuration, so the accelerated image variant changes one variable; reality
filters it, so a CPU-only build handed a GPU host's list still starts.

No model is opened here — that is the integration tests' job. Everything below is
arithmetic over options objects.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import onnxruntime as ort
import pytest
from groundstation_support import make_settings

from reachy_groundstation.runtime import CPU_PROVIDER, RuntimeOptions, resolve_providers

_CUDA = "CUDAExecutionProvider"
_TENSORRT = "TensorrtExecutionProvider"


#:= docs/specs/groundstation/index.md#req-027-inference-parallelism-is-bounded-by-configuration
#:% The service MUST constrain each model runtime to a configured number of threads
#:% rather than letting it size itself against the host.
def test_the_configured_thread_counts_reach_the_session_options() -> None:
    """The scenario: four threads asked for, on a host with many more cores."""
    options = RuntimeOptions.from_settings(
        make_settings(inference_intra_op_threads=4, inference_inter_op_threads=1),
    ).session_options()
    assert options.intra_op_num_threads == 4
    assert options.inter_op_num_threads == 1


def test_the_thread_counts_are_not_the_runtime_defaults() -> None:
    """A number that happened to match the default would prove nothing.

    ONNX Runtime spells "size yourself against the host" as zero, so this is the
    value the service must never leave in place — and this test is what stops a
    future refactor quietly restoring it.
    """
    options = RuntimeOptions.from_settings(
        make_settings(inference_intra_op_threads=2, inference_inter_op_threads=3),
    ).session_options()
    assert options.intra_op_num_threads != 0
    assert options.inter_op_num_threads != 0


def test_execution_is_sequential() -> None:
    """A detection graph has almost nothing to overlap, so nothing is scheduled."""
    options = RuntimeOptions.from_settings(make_settings()).session_options()
    assert options.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL


def test_the_default_thread_count_is_the_one_the_documentation_explains() -> None:
    """Four, from the predecessor's measured curve. Change both together."""
    assert RuntimeOptions.from_settings(make_settings()).intra_op_threads == 4


def test_the_provider_list_is_read_as_an_ordered_preference() -> None:
    """Comma-separated, best first, whitespace forgiven."""
    options = RuntimeOptions.from_settings(
        make_settings(inference_providers=f" {_CUDA} , {CPU_PROVIDER} "),
    )
    assert options.providers == (_CUDA, CPU_PROVIDER)


def test_an_empty_provider_list_falls_back_to_the_cpu() -> None:
    """A list of separators is not a list, and the service still has to start."""
    options = RuntimeOptions.from_settings(make_settings(inference_providers=",,"))
    assert options.providers == (CPU_PROVIDER,)


@pytest.mark.parametrize(
    ("requested", "available", "expected"),
    [
        # The accelerated variant on a host that has the accelerator.
        ((_CUDA, CPU_PROVIDER), (_CUDA, CPU_PROVIDER), (_CUDA, CPU_PROVIDER)),
        # The same configuration on a build without it: it still starts.
        ((_CUDA, CPU_PROVIDER), (CPU_PROVIDER,), (CPU_PROVIDER,)),
        # The CPU provider is always reachable, named or not.
        ((_CUDA,), (_CUDA, CPU_PROVIDER), (_CUDA, CPU_PROVIDER)),
        # Order is the operator's preference and is preserved.
        (
            (_TENSORRT, _CUDA),
            (_CUDA, _TENSORRT, CPU_PROVIDER),
            (_TENSORRT, _CUDA, CPU_PROVIDER),
        ),
        # A name repeated is not a provider tried twice.
        ((CPU_PROVIDER, CPU_PROVIDER), (CPU_PROVIDER,), (CPU_PROVIDER,)),
        # Nothing configured is still something runnable.
        ((), (CPU_PROVIDER,), (CPU_PROVIDER,)),
    ],
)
def test_providers_are_configuration_filtered_by_what_this_build_has(
    requested: tuple[str, ...],
    available: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    """Configuration says what is wanted; the build says what is possible.

    Args:
        requested: What the operator configured.
        available: What this ONNX Runtime build offers.
        expected: What should be handed to the session.
    """
    assert resolve_providers(requested, available) == expected


def test_this_build_can_run_on_the_cpu() -> None:
    """The fallback is only a fallback if it is actually there.

    Every other test in this file compares strings. This one asks the installed
    runtime, because a wheel without a CPU provider would make the whole
    fallback story false and no amount of list arithmetic would notice.
    """
    assert CPU_PROVIDER in ort.get_available_providers()
