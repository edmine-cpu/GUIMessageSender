import asyncio
from types import SimpleNamespace

from parser import GroupParser


class _FakeDB:
    def __init__(self):
        self.saved = []

    def save_parsed_users(self, users):
        self.saved.extend(users)


class _FakeClient:
    def __init__(self):
        self.yielded = 0

    async def iter_participants(self, group, search=None):
        for idx in range(1, 4):
            self.yielded += 1
            yield SimpleNamespace(
                id=idx,
                username=f"user{idx}",
                first_name=f"User{idx}",
                last_name=None,
                phone=None,
                status=None,
                bot=False,
                deleted=False,
            )


def test_parse_group_stops_and_keeps_partial_result():
    db = _FakeDB()
    client = _FakeClient()

    parser = GroupParser(
        client,
        db,
        stop_requested=lambda: client.yielded >= 2,
        progress_cb=lambda msg: None,
    )

    count = asyncio.run(parser.parse_group("@group"))

    assert count == 1
    assert len(db.saved) == 1
    assert db.saved[0].username == "user1"
