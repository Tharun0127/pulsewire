# Decisions

Every important choice in Pulsewire, the alternatives, and why.

## Topology

### Two API instances, one uvicorn worker each, behind nginx
- **Why one worker per instance:** each instance keeps an in-memory registry of its open sockets. With one worker, "connected to api-1" means exactly one process, which is what the cross-server demo and tests show. Scaling out means more instances (containers), which is how real-time services usually scale.
- **Only api-1 runs migrations and the seed** (`RUN_MIGRATIONS=true`), and api-2 starts after api-1 is healthy, so two instances never race on Alembic.

### nginx: `least_conn` + shared `zone`, no sticky sessions
- **No sticky sessions:** any instance can serve any user, because all room events go through Redis pub/sub. Sticky sessions would hide scaling bugs and make rebalancing harder.
- **`least_conn`:** WebSockets stay open for hours, so "fewest open connections" reflects real load better than round robin, which counts requests.
- **`zone`:** nginx runs one worker per CPU, and each worker has its own balancing state. Without a shared-memory zone we measured **10 of 10** requests landing on api-1, because every fresh connection hit a worker whose first pick was api-1. With the zone: 5/5.
- **WebSocket upgrade:** `proxy_http_version 1.1` plus explicit `Upgrade` / `Connection` headers (they are hop-by-hop, so nginx drops them unless told to forward them), and `proxy_read_timeout 3600s` because nginx otherwise closes a proxied connection after 60 s without data.
- **`/direct/api-1/` and `/direct/api-2/`** routes exist only so the demo (and the load test) can pin a client to a specific instance.

## Auth

- JWT with `sub` and `name`, bcrypt passwords hashed in a thread. Stateless tokens mean any instance can verify any user without shared session storage.

## Data model

- **`rooms.last_seq`** is the per-room message counter (see "Ordering").
- **Receipts as two numbers per member** (`last_delivered_seq`, `last_read_seq`) instead of a row per message per member. Because messages in a room are totally ordered, "read up to 57" says everything, and it costs one row update instead of one insert per message.
- **`UNIQUE (room_id, user_id, client_msg_id)`** makes sending idempotent: a client that re-sends after a reconnect gets the original message back instead of a duplicate.

## Testing

- **Each test starts two real uvicorn servers in-process** (`test-1`, `test-2`) on random ports, sharing a test Postgres DB and Redis DB 15, like api-1 and api-2 share `db` and `redis`. Clients use the real `websockets` library over TCP. That tests the actual network path (handshake, auth, pub/sub hop) rather than an in-memory shortcut.

## WebSocket lifecycle

- **Auth on connect with the JWT in the query string** (`/ws?token=...`): browsers cannot set an `Authorization` header on a WebSocket. A bad token is accepted, then closed with code **4401**, so the client can tell "do not retry, log in again" apart from a network drop. (Rejecting the handshake with HTTP 403 would look like any other connection failure to the browser.)
- **One socket per browser tab, multiplexing all rooms.** On connect the server subscribes the socket to every room the user is a member of; after joining a new room over REST, the client sends `subscribe`.
- **Per-connection outgoing queue + writer task.** Broadcasting means putting the event on N queues, which never blocks, so one slow phone cannot delay the whole room. A full queue (1,000 events) closes that socket with **4408**; the client reconnects and catches up from history instead of the server buffering unbounded data.
- **Cleanup in `finally`:** clean close, network drop, server close or crash all run the same code: unregister from rooms (unsubscribing the room channel when the last local socket leaves), stop the writer, update presence.

## Ordering and persistence

- **Persist first, then publish.** Anything a client has seen is already in Postgres, so a client that missed a broadcast can always fetch it.
- **Per-room seq from `UPDATE rooms SET last_seq = last_seq + 1 RETURNING last_seq`** in the same transaction as the insert. The row lock serialises senders in one room (seq N, N+1, no gaps, no duplicates), while different rooms never block each other. Tested with 10 users sending 50 messages at once: seqs 1..50 exactly.
- **Alternatives:** a global `BIGSERIAL` id (unique, but not gapless per room, so clients cannot detect a missing message); a Redis `INCR` per room (fast, but can disagree with the database if the insert fails afterwards, leaving a gap).
- **Idempotent send** via `client_msg_id`: a resend is acked with the original seq and **not** re-broadcast.

## History

- **Cursor pagination on seq** (`before_seq` scrolls up, `after_seq` catches up), not OFFSET. New messages arrive while you scroll, so offsets shift and you would see duplicates or skip messages. `seq < X` always means the same rows, and the `(room_id, seq)` unique index turns every page into an index range scan.

## Horizontal scaling: Redis pub/sub

### One channel per room, subscribed only while a local socket is in the room
- **How:** every room event goes `PUBLISH room:<id>`; each instance's single pub/sub connection subscribes to `room:<id>` when its **first** local socket joins that room and unsubscribes when the **last** leaves. Tested with `PUBSUB NUMSUB`.
- **Every event takes the bus path, even for same-instance receivers.** One code path means the single-instance case is not special and cannot hide bugs.
- **Every message carries `served_by`** (the instance that persisted it) **and `delivered_via`** (the instance that pushed it to this socket). The cross-server test asserts `served_by=test-1, delivered_via=test-2`.
- **Control experiment:** with `USE_REDIS_BUS=false` the same test shows Bob on api-2 never receives Alice's message. That is exactly the bug a naive "store sockets in a dict" chat has once you add a second server.

### Alternatives considered
| Option | Why not (here) |
|---|---|
| Sticky sessions only | Users in one room still land on different servers; it only helps if everyone in a room is pinned to one box. |
| One global channel for everything | Every instance receives every event of every room and filters it, which wastes CPU and bandwidth as rooms grow. |
| Channel per user | Fan-out happens at publish time (N publishes per message for N members); per-room channels publish once. |
| Instances call each other directly (HTTP/gRPC mesh) | Each instance must know the others and who is where; Redis is the rendezvous point we already run. |
| Redis Streams / Kafka | See limitations below. Right for durability, heavier than needed here. |

### Limitations of pub/sub, and what would fix them at larger scale
- **At-most-once, fire-and-forget.** If an instance is restarting, or briefly disconnected from Redis at the moment of `PUBLISH`, its sockets never receive that event. Pub/sub keeps no history.
- **Why that is acceptable here:** messages are persisted **before** publishing, every message has a gapless per-room `seq`, and clients detect gaps and fetch `after_seq` from history on reconnect. A lost broadcast costs a little latency, never data. Typing and presence events are ephemeral anyway.
- **Redis Streams** (`XADD` per room, consumer reads with `XREAD` from a last ID) would make the bus itself replayable: an instance that comes back resumes from the last stream ID it saw, with trimming via `MAXLEN`.
- **Kafka** (topic partitioned by room id) adds durable, replicated, ordered logs, with consumer groups and replay measured in days, and scales to very high message rates across many brokers. The cost is a heavier system to run, and per-partition (not per-room) ordering unless rooms map to partitions.
- **A single Redis is also a single point of failure and a throughput ceiling.** Next steps would be Redis Sentinel/Cluster (sharded pub/sub in Redis 7 with `SSUBSCRIBE`) or moving to one of the above.
