"""Auth and rooms over REST, against two instances."""
from tests.conftest import rest


async def test_signup_on_one_instance_login_on_the_other(start_servers):
    api1, api2 = await start_servers(2)
    async with rest(api1) as c1, rest(api2) as c2:
        signup = await c1.post("/auth/signup", json={"username": "Alice", "password": "password123"})
        assert signup.status_code == 201 and signup.json()["username"] == "alice"
        # Stateless JWT + shared DB: any instance can log any user in.
        login = await c2.post("/auth/login", json={"username": "alice", "password": "password123"})
        assert login.status_code == 200


async def test_duplicate_username_is_rejected(start_servers):
    (api1,) = await start_servers(1)
    async with rest(api1) as client:
        body = {"username": "bob", "password": "password123"}
        assert (await client.post("/auth/signup", json=body)).status_code == 201
        assert (await client.post("/auth/signup", json=body)).status_code == 409


async def test_create_list_and_join_rooms(start_servers, make_user):
    (api1,) = await start_servers(1)
    _, alice = await make_user("alice")
    _, bob = await make_user("bob")
    async with rest(api1, alice) as a, rest(api1, bob) as b:
        room = (await a.post("/rooms", json={"name": "general"})).json()
        assert room["is_member"] is True
        assert (await b.get("/rooms")).json()[0]["is_member"] is False
        assert (await b.post(f"/rooms/{room['id']}/join")).status_code == 204
        assert (await b.post(f"/rooms/{room['id']}/join")).status_code == 204  # idempotent
        listed = (await b.get("/rooms")).json()[0]
        assert listed["is_member"] is True and listed["member_count"] == 2


async def test_rooms_require_auth(start_servers):
    (api1,) = await start_servers(1)
    async with rest(api1) as client:
        assert (await client.get("/rooms")).status_code == 401
