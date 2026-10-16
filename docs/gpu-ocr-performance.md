# GPU OCR acceleration

Windows now prefers bundled DirectML for local OCR, with bounded CPU fallback
when a device is unavailable. No CUDA toolkit or model download is required.
`WUWA_OCR_ACCELERATION=cpu` selects CPU; `directml` requires working GPU sessions
and fails rather than silently measuring CPU. The default is `auto`.

Resource policy: one serialized inference and one latest pending live packet;
GPU recognition batch size one; CPU inference at most four threads. Exact ROI
cache avoids repeated inference. Outside relevant echo pages, classification
has a two-second cooldown even when the scene changes; explicit recheck resets
it. The existing stability gate, uncertainty thresholds and manual mode remain.
GPU failure switches both active models to CPU and invalidates cached results.
The offline self-test prints actual session providers. Packaging audits require
DirectML.dll; startup is still lazy.

Same-machine RTX4070, 11 local private samples, three rounds, cache disabled:
CPU/DirectML1.24.4 warm median classification+parse 500.84/286.73ms (~43% lower).
Process CPU median 2546.88/328.13ms. Batch-one variant 287.22ms, CPU289.06ms,
private bytes ~1.54GB versus ~1.69GB for GPU batch-six. These are process private
bytes, not VRAM. Semantic fields (including nulls/raw field strings) agree for
all 33 cases; raw floating-point confidences can differ slightly. The first GPU
initialization is slower (~2s in the exploratory workload). This does not measure
capture, the >=600ms stability wait, scoring, physical interaction latency, or
concurrent game FPS. No claim of zero gaming impact or universal speedup.

A synthetic ONNX trace recorded 6540 DirectML node events and 40 CPU node events;
GPU session registration alone is not the execution proof. Global GPU memory
rose 139 MiB during that short trace; it is not process-isolated VRAM accounting.
Reducing full-panel detector geometry changed 3/11 sample results and was rejected.

Reproduce with scripts/benchmark_ocr_acceleration.py --acceleration cpu/directml
--samples <local-PNG-directory> --output <local-report.json>, then pass the CPU
report as --baseline for GPU. Reports store hashes/timing, never image or OCR text.
Private samples and profiling traces remain local and ignored.

DirectML.dll 1.15.4 is a Microsoft Windows redistributable under its own
[license](https://www.nuget.org/packages/Microsoft.AI.DirectML/1.15.4/License),
bundled in `_internal/legal/DirectML-LICENSE.txt`. ONNX Runtime telemetry is
disabled before session creation. This does not alter Windows telemetry settings.

A 180-second paced offline runtime test completed 26 saves and repeated stop/start
cycles with one model initialization, no errors, bounded queues, healthy temporary
database and one Python thread after stop. This is a bounded test, not an all-day
memory or concurrent-game-FPS guarantee. CPU/GPU replay also retained identical
scores, projections and decisions for 20 accepted cases.
