from app.services import memory_compaction_service


def _count_tokens(messages: list) -> int:
    return len(messages) * 10


def test_truncate_messages_to_limit_removes_oldest_until_minimum():
    messages = [
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "m2"},
        {"role": "user", "content": "m3"},
    ]

    final_context, final_tokens, removed_messages, removed_tokens = (
        memory_compaction_service._truncate_messages_to_limit(
            summary="",
            messages=messages,
            new_user_message="latest",
            limit=15,
            min_remaining_messages=1,
            token_counter=_count_tokens,
        )
    )

    assert removed_messages == 2
    assert removed_tokens == 20
    assert final_tokens == 20
    assert len(messages) == 1
    assert final_context[-1] == {"role": "user", "content": "latest"}


def test_truncate_context_without_summary_removes_summary_for_hard_limit():
    result = memory_compaction_service.truncate_context_without_summary(
        summary="old summary",
        messages=[],
        new_user_message="latest",
        soft_limit=10,
        hard_limit=1,
        min_recent=0,
        token_counter=_count_tokens,
    )

    assert result["summary"] == ""
    assert result["memory_changed"] is True
    assert result["final_tokens"] == 10
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["trigger"] == "hard_budget_truncate"
    assert event["summary_text"] == "old summary"


def test_truncate_event_fields():
    event = memory_compaction_service._truncate_event(
        trigger="soft_budget_truncate",
        removed_messages=3,
        removed_tokens=42,
        before_tokens=100,
        after_tokens=58,
        summary_text="",
    )

    assert event == {
        "trigger": "soft_budget_truncate",
        "removed_messages": 3,
        "removed_tokens": 42,
        "before_tokens": 100,
        "after_tokens": 58,
        "summary_text": "",
    }
