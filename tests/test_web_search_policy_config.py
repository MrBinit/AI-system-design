from pathlib import Path

import yaml


def _load_web_search_config() -> dict:
    config_path = Path(__file__).resolve().parents[1] / "app" / "config" / "tavily_config.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return payload.get("web_search", {}) if isinstance(payload, dict) else {}


def test_web_search_policy_deep_has_stronger_coverage_floor():
    web = _load_web_search_config()
    assert int(web.get("retrieval_min_unique_domains", 1)) >= 1
    assert int(web.get("deep_min_unique_domains", 1)) >= 2
    assert int(web.get("deep_min_unique_domains", 1)) >= int(
        web.get("retrieval_min_unique_domains", 1)
    )


def test_web_search_policy_official_sources_enabled():
    web = _load_web_search_config()
    assert bool(web.get("official_source_filter_enabled", False)) is True
    allowlist = web.get("official_source_allowlist", [])
    assert isinstance(allowlist, list)
    assert "daad.de" in [str(item).strip().lower() for item in allowlist]


def test_web_search_policy_deep_knobs_not_weaker_than_standard():
    web = _load_web_search_config()
    assert int(web.get("deep_max_query_variants", 1)) >= int(web.get("max_query_variants", 1))
    assert int(web.get("deep_max_context_results", 1)) >= int(web.get("max_context_results", 1))
    assert int(web.get("deep_default_num", 1)) >= int(web.get("default_num", 1))
    assert int(web.get("deep_max_pages_to_fetch", 0)) >= int(web.get("max_pages_to_fetch", 0))
    assert int(web.get("deep_max_chunks_per_page", 1)) >= int(web.get("max_chunks_per_page", 1))
