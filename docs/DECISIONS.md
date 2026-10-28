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
