from pathlib import Path

from motionledger.pipeline.cache import ArtifactCache


def test_artifact_cache_roundtrip_and_copy(tmp_path: Path):
    cache = ArtifactCache(tmp_path / "cache")
    key = cache.key("sam", {"video": "abc", "concept": "orange gasket"})
    cache.write_json(key, {"response_id": "resp_1", "records": 12})
    source = tmp_path / "stream.jsonl"
    source.write_text('{"type":"response.completed"}\n', encoding="utf-8")
    cache.store_file(key, "events.jsonl", source)
    target = tmp_path / "run" / "events.jsonl"

    assert cache.read_json(key) == {"response_id": "resp_1", "records": 12}
    assert cache.has_files(key, ["events.jsonl"])
    assert cache.copy_file(key, "events.jsonl", target)
    assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert cache.read_json("f" * 64) is None


def test_spark_cost_uses_cached_input_discount():
    from motionledger.pipeline.runner import _spark_cost
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000,
             "input_tokens_details": {"cached_tokens": 400_000}}
    assert round(_spark_cost(usage), 4) == round((600_000 * 1.25 + 400_000 * .15 + 100_000 * 4.25) / 1_000_000, 4)
