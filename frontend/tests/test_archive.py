from helpers import stats
from signalgrid_frontend.archive import ArchiveClient, Backfiller


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        return FakeResponse(self.payload)


def test_history_queries_archive_with_bounded_limit():
    session = FakeSession([stats(0).model_dump(), stats(500).model_dump()])
    client = ArchiveClient("http://archive:8003/", session=session)
    result = client.history("StationA", 0, limit=50)
    assert [r.timestamp for r in result] == [0, 500]
    assert session.calls == [("http://archive:8003/stats", {"station": "StationA", "sensor": 0, "limit": 50})]


def test_backfiller_merges_history_into_store(store):
    store.add_stats(stats(1_000))
    session = FakeSession([stats(0).model_dump(), stats(500).model_dump()])
    Backfiller(ArchiveClient("http://a", session=session), store)._run(("StationA", 0))
    assert store.series("StationA", 0).timestamps == [0, 500, 1_000]
