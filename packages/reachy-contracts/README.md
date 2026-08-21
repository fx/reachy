# reachy-contracts

Shared wire types and golden fixtures for the Reachy Mini stack.

Every component that speaks the robot-link protocol depends on this package by
path, so a wire type has exactly one definition and the golden fixtures that pin
it are the same on both sides of the connection.

```python
from reachy_contracts import (
    CaptureTimestamp,
    FaceDetections,
    FrameHeader,
    ResultEnvelope,
    fixture_bytes,
    round_trip,
)

header = FrameHeader(sequence=41, captured_at=CaptureTimestamp("3894112233445566"))
result = ResultEnvelope[FaceDetections].for_frame(header, "face", FaceDetections())
raw = result.to_wire()
```

Three modules, three concerns:

| Module | What it holds |
|---|---|
| `reachy_contracts.session` | Negotiation, the frame header, the result envelope, errors and close |
| `reachy_contracts.values` | The wire model base, normalised coordinates, the capture token, and the per-capability payloads |
| `reachy_contracts.fixtures` | The golden corpus in `golden/` and the loader every consumer reads it through |

Everything public is re-exported from `reachy_contracts` itself; consumers import
from there rather than reaching into a submodule.

`just contracts` regenerates the JSON Schema for every message type into
`docs/contracts/robot-link/`, and the contract-drift gate fails on any
difference between that output and the committed copy.
