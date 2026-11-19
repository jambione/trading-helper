import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import quotes_parallel as Q  # noqa: E402


def test_shards_partition_the_plan():
    plan = [("x", "2026-05-01", "S%d" % i, i, 0.0) for i in range(200)]
    parts = [Q.shard(plan, i, 3) for i in range(3)]
    assert sum(len(p) for p in parts) == 200 and len(set().union(*map(set, parts))) == 200


def test_merge_keeps_existing_and_adds_parts(tmp_path, monkeypatch):
    main = tmp_path / "quotes.json"
    main.write_text(json.dumps({"a": 1.0}))
    monkeypatch.setattr(Q.ns, "QUOTES", main)
    p1 = tmp_path / "p1.json"; p1.write_text(json.dumps({"b": 2.0}))
    assert Q.merge([p1, tmp_path / "missing.json"]) == 2
    assert json.load(open(main)) == {"a": 1.0, "b": 2.0}
