from app.services import evaluation_service


class FakeRedis:
    def __init__(self):
        self._kv = {}
        self._lists = {}

    def setex(self, key, _ttl, value):
        self._kv[key] = value

    def get(self, key):
        return self._kv.get(key)

    def lpush(self, key, value):
        self._lists.setdefault(key, [])
        self._lists[key].insert(0, value)

    def ltrim(self, key, start, end):
        values = self._lists.get(key, [])
        self._lists[key] = values[start : end + 1]

    def expire(self, _key, _ttl):
        return True

    def lrange(self, key, start, end):
        values = self._lists.get(key, [])
        return values[start : end + 1]


def test_store_and_list_chat_traces():
    redis = FakeRedis()
    conversation_id = evaluation_service.store_chat_trace(
        user_id="user-1",
        prompt="What is RTU?",
        answer="A public research university in Munich.",
        retrieved_results=[
            {"chunk_id": "university_1:0000", "content": "Rheinberg Technical University (RTU) ..."}
        ],
        retrieval_strategy="filtered_exact",
        timings_ms={"retrieval": 10},
        redis=redis,
    )

    assert conversation_id is not None
    stored_raw = redis._kv[evaluation_service._conversation_key(conversation_id)]
    assert stored_raw.startswith("enc:v2:")
    traces = evaluation_service.list_chat_traces("user-1", limit=10, redis=redis)
    assert len(traces) == 1
    assert traces[0]["conversation_id"] == conversation_id
    assert traces[0]["prompt"] == "What is RTU?"


def test_label_chat_trace_and_report():
    redis = FakeRedis()
    conversation_id = evaluation_service.store_chat_trace(
        user_id="user-2",
        prompt="Where is RTU located?",
        answer="RTU is in Munich, Germany.",
        retrieved_results=[
            {
                "chunk_id": "university_1:0000",
                "content": "Location: Munich, Germany",
            }
        ],
        retrieval_strategy="ann",
        timings_ms={"retrieval": 8},
        redis=redis,
    )

    labeled = evaluation_service.label_chat_trace(
        user_id="user-2",
        conversation_id=conversation_id,
        expected_answer="RTU is located in Munich, Germany.",
        relevant_chunk_ids=["university_1:0000"],
        redis=redis,
    )

    assert labeled is not None
    report = evaluation_service.get_user_evaluation_report("user-2", limit=10, redis=redis)

    assert report["total_conversations"] == 1
    assert report["labeled_conversations"] == 1
    assert report["retrieval_metrics"]["hit_at_k"] == 1.0
    assert report["generation_metrics"]["query_relevance"] > 0.0
    assert report["conversations"][0]["metrics"]["generation"]["hallucination_proxy"] < 1.0
