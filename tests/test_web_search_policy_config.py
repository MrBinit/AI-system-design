from pathlib import Path

import yaml


def _load_web_search_config() -> dict:
    config_path = Path(__file__).resolve().parents[1] / "app" / "config" / "tavily_config.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return payload.get("web_search", {}) if isinstance(payload, dict) else {}


def test_web_search_policy_phase1_limits_are_bounded():
    web = _load_web_search_config()
    assert int(web.get("phase1_max_queries", 0)) == 5
    assert int(web.get("phase1_max_results_per_query", 0)) == 3
    assert int(web.get("phase1_max_total_urls_to_fetch", 0)) == 8
    assert int(web.get("phase1_max_pdfs_to_read", 0)) == 3
    assert int(web.get("phase1_max_pdf_size_mb", 0)) == 15
    assert int(web.get("phase1_max_pdf_pages", 0)) == 40


def test_web_search_policy_official_sources_enabled():
    web = _load_web_search_config()
    assert bool(web.get("official_source_filter_enabled", False)) is True
    allowlist = web.get("official_source_allowlist", [])
    assert isinstance(allowlist, list)
    assert "daad.de" in [str(item).strip().lower() for item in allowlist]


def test_web_search_policy_legacy_deep_knobs_removed_from_file():
    web = _load_web_search_config()
    legacy_keys = [
        "query_planner_enabled",
        "query_planner_use_llm",
        "retrieval_loop_enabled",
        "retrieval_loop_use_llm",
        "deep_total_query_budget",
        "deep_internal_crawl_enabled",
        "deep_required_field_rescue_enabled",
    ]
    assert not any(key in web for key in legacy_keys)
