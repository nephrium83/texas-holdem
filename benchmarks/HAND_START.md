# Complete hand-start measurements (#16)

Run from the checkout with the development dependencies and verified native
crypto available:

```shell
python benchmarks/run_hand_start.py --hands 20 --output-dir performance-report
```

The output directory must be new. Each seat configuration starts a fresh
process. The first hand is recorded separately from 20 warm hands. This
measures a cold first hand after imports/native initialization, **not**
end-to-end application startup. p95 uses nearest rank, `ceil(.95 * N)`.

Three complementary harnesses are preserved:

- `mental_deal_startup.py`: coordinator work and logical JSON byte counts,
  detection-only versus Bayer–Groth prevention. Existing security parameters
  and transcript construction are unchanged.
- `session_hand_start.py`: real signed, admitted Sessions, using the existing
  production-mode test arrangement. Signed-envelope bytes count serialization
  once per emitted envelope, not host fan-out, framing, retries or upload.
  Envelope verification occurs once per message in this bus; production
  receivers each verify independently. Identity setup is excluded from timing.
- `critical_path.py`: **modeled** parallel wait from measured coordinator
  wall costs, replayed at several latencies. It assumes equal seat hardware,
  fixed FIFO processing and message latency; it excludes bandwidth contention,
  envelope costs, transport overhead and heterogeneous machines. Its estimates
  are not measured Internet latency or a proven bound on production wait.

Peak RSS uses the operating system's process high-water measurement, including
native crypto, setup and caches. The orchestrator isolates seat configurations
so the value cannot inherit a larger previous seat count. The coordinator
comparison retains both policies in one process; its memory peak is cumulative
within that comparison. It is aggregate harness memory, not memory per player.
Metadata records source commit, Python, OS, CPU count and libsodium version.

The **Hand-start performance** workflow runs the 2/4/9-seat matrix on CI,
publishes JSON/raw logs and `REPORT.md` as a run artifact, and adds the report
to the run summary. This is a baseline, not a timing gate on shared runners.

Claude's BWING measurements at `1d3411f` (20 warm hands) found modeled p95
waits of 0.41/0.87/1.85 seconds at zero latency and 0.81/1.46/2.95 seconds
at 100 ms per message for 2/4/9 seats. These meet the stated budgets **on
that hardware/model**. Sequential proof generation dominates the critical
path; aggregate verification CPU is not the same bottleneck as player wait.
See the [measurement evidence](https://github.com/nephrium83/texas-holdem/issues/16#issuecomment-6969256362).

Still required: actual host upload bytes and elapsed time during the paired
two-home-network test, including which network layer was counted. The roughly
1.5 MB / 1.2 s at 10 Mbit/s estimate is not a measurement. Do not check off
real transport or change the product's prevention policy based on this model.
