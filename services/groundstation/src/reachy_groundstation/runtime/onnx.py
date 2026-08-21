"""Model-runtime sessions: which provider, how many threads, and warmed up.

One class and one function, and both exist because of the same requirement.
Groundstation REQ-027 says the service constrains each model runtime to a
configured number of threads rather than letting it size itself against the
host, and a runtime left to its own devices does exactly that — ONNX Runtime
defaults its intra-operator pool to the number of cores it can see, so a
groundstation sharing a large host with anything else takes the whole machine
for a 40 ms detection.

Two further decisions are worth stating, because neither is visible from the
signatures.

**Inference runs on a thread of its own, one at a time.** A detection pass is
tens of milliseconds of blocking C++, and running it on the event loop would
stall every other session in the process for that long. It runs on a dedicated
single-worker executor instead: off the loop, and serialised, so the thread
bound above is the bound on this runtime's whole appetite rather than the bound
per concurrent call. A second session does not double the CPU a model uses; it
waits.

**Provider selection is configuration filtered by reality.** The requested
providers are tried in the order given, anything this build has not got is
dropped, and the CPU provider is appended if it is not already there. That is
what lets the accelerated image variant change one environment variable and
nothing else, and what stops a CPU-only build failing to start because it was
handed a provider list from a GPU host.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self

import numpy as np
import onnxruntime as ort

from reachy_groundstation.obs import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    import numpy.typing as npt

    from reachy_groundstation.config import Settings

__all__ = [
    "CPU_PROVIDER",
    "ModelRuntime",
    "RuntimeOptions",
    "resolve_providers",
]

_logger = get_logger(__name__)

# The provider every build has, and therefore the one the list always ends at.
CPU_PROVIDER: Final = "CPUExecutionProvider"

# Tensors are float32 throughout: YuNet's input is a float32 blob and every one
# of its heads is float32. The alias exists so the signatures below say that
# once rather than spelling numpy's version of it four times.
type _Tensor = "npt.NDArray[np.float32]"


def resolve_providers(
    requested: Iterable[str],
    available: Iterable[str],
) -> tuple[str, ...]:
    """Reduce a configured provider preference to what this build can honour.

    Args:
        requested: The providers to try, best first.
        available: What this ONNX Runtime build offers.

    Returns:
        The requested providers that exist here, in the order requested, with
        the CPU provider last if it was not named. Duplicates are removed, so a
        list that names the CPU provider twice is not a list that tries it
        twice.
    """
    offered = set(available)
    resolved: list[str] = []
    for provider in requested:
        if provider in offered and provider not in resolved:
            resolved.append(provider)
    if CPU_PROVIDER not in resolved:
        resolved.append(CPU_PROVIDER)
    return tuple(resolved)


#:= docs/specs/groundstation/index.md#req-027-inference-parallelism-is-bounded-by-configuration
#:% The service MUST constrain each model runtime to a configured number of threads
#:% rather than letting it size itself against the host.
@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """How a model runtime is to be built.

    Attributes:
        intra_op_threads: Threads one operator may spread across.
        inter_op_threads: Operators that may run at once.
        providers: Execution providers to try, best first, before filtering.
    """

    intra_op_threads: int
    inter_op_threads: int
    providers: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Read the options out of the service's configuration.

        Args:
            settings: The settings in effect.

        Returns:
            The options, with the provider list split on commas and stripped.
        """
        providers = tuple(
            part.strip()
            for part in settings.inference_providers.split(",")
            if part.strip()
        )
        return cls(
            intra_op_threads=settings.inference_intra_op_threads,
            inter_op_threads=settings.inference_inter_op_threads,
            providers=providers or (CPU_PROVIDER,),
        )

    def session_options(self) -> ort.SessionOptions:
        """Render the thread bounds as ONNX Runtime session options.

        Returns:
            Options that pin both thread pools to the configured sizes.
        """
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.intra_op_threads
        options.inter_op_num_threads = self.inter_op_threads
        # Sequential rather than parallel execution: these are single-branch
        # detection graphs, so the parallel scheduler has almost nothing to
        # overlap and spends the inter-operator threads on coordinating instead.
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return options


class ModelRuntime:
    """One loaded model, run off the event loop and one call at a time."""

    def __init__(self, model_path: Path, options: RuntimeOptions, name: str) -> None:
        """Load a model and bound what running it may use.

        Args:
            model_path: The file to load. It is already in the artifact —
                nothing here fetches anything.
            options: The thread bounds and provider preference.
            name: What to call this runtime in logs and in its thread's name.
        """
        self._name = name
        providers = resolve_providers(options.providers, ort.get_available_providers())
        self._session = ort.InferenceSession(
            str(model_path),
            options.session_options(),
            providers=list(providers),
        )
        self._input_name: str = self._session.get_inputs()[0].name
        self._output_names: tuple[str, ...] = tuple(
            output.name for output in self._session.get_outputs()
        )
        # One worker, so this runtime's concurrency is one inference and its CPU
        # appetite is the thread bound above rather than a multiple of it.
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"inference-{name}",
        )
        _logger.info(
            "runtime.loaded",
            model=name,
            providers=list(providers),
            intra_op_threads=options.intra_op_threads,
            inter_op_threads=options.inter_op_threads,
        )

    @property
    def input_name(self) -> str:
        """The name of the model's single input tensor.

        Returns:
            The input name, as the model declares it.
        """
        return self._input_name

    def run(self, feed: Mapping[str, _Tensor]) -> dict[str, _Tensor]:
        """Run the model, blocking the calling thread.

        Args:
            feed: Input tensors by name.

        Returns:
            Every output, by name.
        """
        outputs: Sequence[_Tensor] = self._session.run(None, dict(feed))
        return dict(zip(self._output_names, outputs, strict=True))

    async def infer(self, feed: Mapping[str, _Tensor]) -> dict[str, _Tensor]:
        """Run the model on this runtime's own thread.

        Args:
            feed: Input tensors by name.

        Returns:
            Every output, by name.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.run, feed)

    #:= docs/specs/groundstation/index.md#req-026-readiness-is-distinct-from-liveness
    #:% The service MUST report itself ready only once every capability it will offer
    #:% has completed its warm-up.
    async def warm_up(self, shape: tuple[int, ...]) -> None:
        """Pay the first inference's cost before anything is waiting on it.

        The first call into a freshly opened session allocates its arenas, plans
        its kernels and spins up its thread pool, which is most of the reason a
        cold first frame is slow. Doing it here is what makes readiness mean
        what groundstation REQ-026 says it means.

        Args:
            shape: The input shape to warm up at, in the model's own layout.
        """
        await self.infer({self._input_name: np.zeros(shape, dtype=np.float32)})

    def close(self) -> None:
        """Release the thread this runtime holds.

        Synchronous, because the caller is not always inside an event loop: the
        capability closes through `aclose`, and a test that only ever ran the
        model on the calling thread has no loop to close it from.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        """Release the thread this runtime holds."""
        self.close()
