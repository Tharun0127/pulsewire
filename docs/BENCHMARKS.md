# Pulsewire benchmarks

Setup: Windows 11 laptop, Docker Desktop (16 vCPU, 8 GB VM). Two API
instances (one uvicorn worker each) behind nginx, Postgres 16, Redis 7. The
load generator ([`benchmarks/load.py`](../backend/benchmarks/load.py)) runs
in a **separate one-off container** on the same Docker network, connecting
through the nginx load balancer.

```bash
docker compose run --rm --no-deps -e BENCH_TARGET=http://nginx api-1 python -m benchmarks.load           # steady
docker compose run --rm --no-deps -e BENCH_TARGET=http://nginx api-1 python -m benchmarks.load --burst   # burst
```

Scenario: **1,000 concurrent WebSockets**, 1,000 users in 20 rooms of 50.
2 senders per room send 1 message/s for 30 s (40 msg/s in, 2,000
deliveries/s out). Latency = receive time - send time stamped in the message
body (same machine, same clock), so it covers the full path: client → nginx
→ instance → Postgres → Redis pub/sub → other instance → nginx → client.

## Results

| Metric | Steady (senders spread over each second) | Burst (all 40 senders fire at the same instant) |
|---|---|---|
| Connections per instance (via load balancer) | **500 / 500** | **500 / 500** |
| Deliveries received / expected | **60,000 / 60,000** | **60,000 / 60,000** |
| Delivery rate | 2,000 /s | 2,000 /s |
| Latency p50 | **10.8 ms** | 317 ms |
| Latency p95 | **22.8 ms** | 453 ms |
| Latency p99 | 61.0 ms | 541 ms |
| Memory per instance, idle → 500 sockets | 79 → 162 MB | 79 → 162 MB |
| **Memory per connection** | **~170 KB** | ~169 KB |
| Time to open 1,000 sockets (batches of 100) | ~9 s | ~9 s |

Raw output: [`load_1000_steady.json`](../backend/benchmarks/results/load_1000_steady.json),
[`load_1000_burst.json`](../backend/benchmarks/results/load_1000_burst.json).

## What the numbers say

- **Horizontal scaling works and is lossless.** `least_conn` with a shared
  zone split 1,000 sockets exactly 500/500, and every one of the 60,000
  expected deliveries arrived. About half of them crossed instances through
  Redis pub/sub.
- **Steady traffic is fast:** p50 11 ms and p95 23 ms end to end, including a
  Postgres commit before every publish.
- **Bursts are the real limit.** When all 40 senders fire in the same
  millisecond, each instance has ~1,000 WebSocket frames to write at once,
  and the single-process client has 2,000 to read. Per-frame Python cost
  (framing, syscalls, event-loop scheduling) on both ends makes the queue
  take ~300 ms to drain. Throughput over the second is unchanged; only the
  arrival time spreads out.
- **An optimisation that did not help (and why it was kept):** we changed
  fan-out to serialise each event once per instance instead of once per
  socket (1 `json.dumps` instead of 25). Burst p50 went 312 → 317 ms, which
  is within noise, so JSON encoding was not the bottleneck. It stays because
  it is simpler and cheaper, but the docs do not claim a win.
- **What would cut burst latency:** more instances (fewer sockets each, so
  smaller per-instance bursts; pub/sub already supports it), a faster
  WebSocket runtime (uvloop, or a Go/Rust edge tier that only does fan-out),
  and batching several events into one frame when a client is behind.
- **Memory: ~170 KB per connection** in Python (socket buffers, the
  `websockets` protocol object, our `Connection` with its queue and writer
  task). 10,000 connections would need about 1.7 GB per instance, so a
  single instance would be RAM-bound well before CPU-bound at steady
  traffic. That is a sizing argument for more, smaller instances.

## Caveats

- Load generator and servers share one laptop. The client process itself
  handles 2,000 incoming frames/s on one core, which contributes to the
  burst latency.
- Nginx, Postgres and Redis all run in the same Docker VM, so "network" hops
  are local. Real deployments add RTT but also spread CPU across machines.
