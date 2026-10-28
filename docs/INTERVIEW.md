# Interview questions: real-time systems, answered with Pulsewire

Each answer is written the way you would say it out loud.

---

### 1. WebSockets vs polling vs SSE: when do you use which?

"Polling means the client keeps asking 'anything new?', so latency is your poll interval and most requests come back empty. Long polling holds the request open until there's news. That's better, but every message still costs a new HTTP request. Server-Sent Events are a one-way stream from server to client, with reconnect built in: perfect for notifications or a live score.

Chat is two-way and chatty: sends, typing, receipts and heartbeats all go client to server. So I used WebSockets: one connection, full-duplex, tiny frames. The price is that connections are stateful, which is what the rest of this project deals with. In a different project I deliberately used 2-second polling for a seat map, because it didn't need a socket."

### 2. Walk me through the life of a WebSocket connection in your app.

"The browser opens `/ws?token=JWT`. The token goes in the query string because browsers can't set headers on a WebSocket. If the token is bad, I close with code 4401, so the client knows not to retry. If it's good, I register the socket in this instance's registry, subscribe to the Redis channels of the user's rooms, and send `hello`. That's always the first frame, and it includes which instance you're on and each room's latest message number.

Then there's a read loop, while writes go through a per-socket queue with its own task, so one slow phone can't block a room. When the socket ends, whether cleanly, by a network drop, by a crash or because the server closed it, one `finally` block unregisters it, unsubscribes rooms no local socket needs any more, and updates presence."

### 3. How do two users on different servers chat?

"Alice is on api-1, Bob is on api-2. When Alice sends, api-1 saves the message to Postgres and then publishes it on a Redis channel for that room, `room:7`. Every instance that has a socket in room 7 is subscribed to that channel, including api-2, which pushes it to Bob. Each message carries `served_by` and `delivered_via`, so the UI literally shows `api-1 → api-2`. My integration test asserts exactly that. There's also a control test with Redis switched off, where Bob never gets the message. That's the bug you get if you just keep sockets in a Python dict and add a second server."

### 4. Why one channel per room, and not one global channel?

"With a global channel, every instance receives every message of every room and throws most of them away, so the cost grows with total traffic. With a channel per room, an instance subscribes only while it has a socket in that room. In my code the hub subscribes when the first local socket joins a room and unsubscribes when the last one leaves, and a test checks that with `PUBSUB NUMSUB`. A channel per user would mean publishing once per member, so 50 publishes for a 50-person room instead of one."

### 5. What are the limits of Redis pub/sub?

"It's fire-and-forget. If api-2 is restarting at the moment api-1 publishes, api-2 never sees that message, because pub/sub has no history. I accept that here because I persist before publishing and every message has a gapless per-room sequence number. A client that reconnects, or that sees seq 10 after seq 8, fetches what it missed from Postgres. So a lost publish costs latency, never data.

At bigger scale I'd move to Redis Streams, where each instance resumes from the last stream ID it saw, or to Kafka partitioned by room, which gives durable, replicated, replayable logs. A single Redis is also a single point of failure, so I'd add Sentinel or Cluster with sharded pub/sub."

### 6. How does nginx handle WebSockets, and did anything go wrong?

"A WebSocket starts as an HTTP request with `Upgrade: websocket`. Those are hop-by-hop headers, so nginx drops them unless you forward them explicitly. You also need HTTP/1.1 upstream, and a long `proxy_read_timeout`, because nginx closes idle proxied connections after 60 seconds by default.

What went wrong: I measured 10 out of 10 requests going to api-1. nginx runs one worker process per CPU, and each worker keeps its own round-robin state, so every fresh connection hit a worker whose first pick was api-1. Adding a shared memory `zone` fixed it. I also switched to `least_conn`, because WebSockets live for hours and open connections are the real load. The load test then split 1,000 sockets exactly 500/500."

### 7. How does WhatsApp show "online", and how did you build it?

"Heartbeats plus a timeout. My client pings every 15 seconds. The server keeps, per user, a Redis sorted set of their connections, scored by when each one expires: now plus 30 seconds, refreshed on every ping. You're online if any of your connections hasn't expired. Closing a tab removes that connection at once, so you go offline instantly if it was your last tab. If your laptop lid closes, no goodbye is ever sent, the pings stop, and within about 30 seconds the entry expires.

I used a sorted set rather than one key per user so that two tabs, even on different servers, don't fight. Closing one tab shouldn't mark you offline."

### 8. Nobody sends an event when a laptop dies. So who notices?

"A small sweeper on each instance. Every 5 seconds it recomputes who's online in the rooms that instance serves and pushes changes to its own sockets only. So the work scales with that instance's connections. It also closes sockets that have sent nothing, not even a ping, for 45 seconds, because a half-open TCP connection can look alive to the server for a long time. My test makes Bob go silent without closing, and checks Alice sees him offline before the idle-close fires. That proves the TTL does the job."

### 9. How do typing indicators work without flooding the server?

"Two throttles. The client sends 'typing' at most once every 2 seconds. The server does `SET typing:room:user NX PX 2000` and only broadcasts if the key was new. That key is in shared Redis, so the throttle holds across instances and against a buggy client: my test sends 10 typing events and sees one broadcast. Receivers show the indicator for 5 seconds after the last event, so there's no 'stopped typing' message to lose."

### 10. How do you guarantee message ordering?

"Each room has a counter, `last_seq`. When a message comes in, one transaction does `UPDATE rooms SET last_seq = last_seq + 1 RETURNING last_seq` and inserts the message with that number. The row lock means two senders in the same room get N and N+1, never the same number and never a gap, while different rooms don't block each other. My test has 10 users fire 50 messages at once and checks the seqs are exactly 1 to 50. Clients display by seq, not by arrival time, so even if two instances deliver slightly out of order, the screen is right."

### 11. How do sent, delivered and read ticks work?

"Sent is the server's ack, which only goes out after the message is in Postgres. Delivered is when the other person's client has it and replies 'delivered'. Read is when it was on their screen. For groups, like WhatsApp, it's delivered or read when every other member got there.

I don't store a row per message per person. Because messages are ordered, I store two numbers per member: 'delivered up to 57' and 'read up to 55'. Updates only ever move forward, using GREATEST in SQL, and they're clamped to the room's last seq, so a late duplicate can't move ticks backwards and a client can't claim to have read the future. The client also batches receipts every 500 ms."

### 12. A user's Wi-Fi drops for a minute. What happens?

"The client sees the socket close and reconnects with exponential backoff and jitter: a random wait between zero and a growing cap. The jitter matters because when a server restarts, thousands of clients drop at the same moment, and without it they'd all retry in sync and hammer the survivor.

On reconnect it connects first, so live messages start arriving, then reads `hello`, which has each room's latest seq, and fetches everything after its last seen seq. Everything is merged by seq, so a message that arrives both live and from history is stored once. My test reconnects Bob to the other server while Alice keeps sending, and checks he ends up with exactly seqs 1 to 6."

### 13. What if a message was sent but the ack never came back?

"The client doesn't know whether the server got it, so it keeps unacked messages and resends them after reconnecting, with the same `client_msg_id`. The database has a unique constraint on room, user and that id. If the first attempt was stored, the server just acks with the original seq and does not broadcast it again. My test kills the TCP connection right after a send, reconnects to the other instance, resends, and checks there's one row and Bob saw it once."

### 14. How did it perform, and what's the bottleneck?

"1,000 concurrent sockets: nginx split them 500/500, and all 60,000 expected deliveries arrived. With senders spread out, p50 was 11 ms and p95 23 ms end to end, including a Postgres commit before every publish. Memory was about 170 KB per connection.

The interesting case is bursts. When all 40 senders fire in the same millisecond, each instance has about a thousand frames to write at once, and p50 rose to around 300 ms. I tried serializing each event once instead of once per socket, expecting a win, and it made no measurable difference, so I don't claim it. The real fixes are more instances, so each one has smaller bursts, a faster WebSocket runtime, or batching frames."

### 15. How would you scale this to millions of users?

"Separate the connection layer from the logic. Gateway servers that only hold sockets and do fan-out, written in something very efficient, autoscaled on connection count, since at 170 KB per connection memory runs out first. Behind them, stateless services that persist messages.

Replace pub/sub with a durable log, Kafka or Redis Streams, partitioned by room, so instances can replay. Presence goes to a sharded store with TTLs, and you only send presence for people on screen, because presence is the chattiest traffic. Shard message storage by room and keep cursor pagination. Add a connection-aware load balancer, and drain servers gracefully on deploys: I already close sockets with 1012 so clients reconnect elsewhere right away, with jitter so they don't stampede. And measure every step, like the load test here."
