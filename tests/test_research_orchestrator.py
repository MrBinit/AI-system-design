from app.services import research_orchestrator as orchestrator


def test_build_research_plan_selects_professor_and_lab_objectives():
    plan = orchestrator.build_research_plan(
        "find professors and research labs for university of bonn data science"
    )

    objective_ids = [str(item.get("id", "")) for item in plan["objectives"]]
    assert "professors_and_supervision" in objective_ids
    assert "labs_and_research" in objective_ids
    assert any("official pdf" in query.lower() for query in plan["queries"])


def test_build_queries_for_missing_objectives_includes_domain_scoped_queries():
    queries = orchestrator.build_queries_for_missing_objectives(
        "university of bonn msc ai",
        missing_objectives=[
            {
                "id": "application_portal_and_contact",
                "query_focus": "application portal contact admissions office email phone",
            }
        ],
        official_domains=["uni-bonn.de", "daad.de"],
        max_queries=10,
    )

    assert queries
    assert any("site:uni-bonn.de" in query for query in queries)
    assert any("official website" in query.lower() for query in queries)


def test_research_objective_coverage_reports_missing_and_covered_objectives():
    objectives = [
        {
            "id": "professors_and_supervision",
            "label": "Professors and supervision",
            "subquestion": "professor list and supervisor options",
            "query_focus": "faculty professors supervisor advisor contact email",
            "coverage_keywords": ("professor", "faculty", "supervisor", "advisor"),
            "min_keyword_hits": 1,
        },
        {
            "id": "application_deadline",
            "label": "Application deadline",
            "subquestion": "application deadline exact date",
            "query_focus": "application deadline intake timeline exact date",
            "coverage_keywords": ("deadline", "apply by", "closing date"),
            "min_keyword_hits": 1,
        },
    ]
    candidates = [
        {
            "content": "Faculty directory: Professor Jane Doe supervises AI research projects.",
            "metadata": {"title": "Faculty", "url": "https://uni-example.de/faculty"},
        }
    ]

    status = orchestrator.research_objective_coverage(objectives, candidates)

    assert status["coverage"] == 0.5
    assert "application_deadline" in status["missing_ids"]
    assert "professors_and_supervision" not in status["missing_ids"]
