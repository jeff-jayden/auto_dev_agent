import os
import json
import io
import unittest
from unittest.mock import patch

from llm.gateway import OpenAICompatibleGateway, OllamaGateway, build_model_gateway
from pydantic import BaseModel


class ModelGatewayConfigurationTests(unittest.TestCase):
    def test_deepseek_provider_uses_official_endpoint_and_disables_thinking(self):
        environment = {
            "MODEL_PROVIDER": "deepseek",
            "MODEL_NAME": "deepseek-flash",
            "DEEPSEEK_API_KEY": "test-key",
        }
        with patch.dict(os.environ, environment, clear=True):
            gateway = build_model_gateway()

        self.assertIsInstance(gateway, OpenAICompatibleGateway)
        self.assertEqual(gateway.base_url, "https://api.deepseek.com")
        self.assertEqual(gateway.model, "deepseek-flash")
        self.assertEqual(gateway.api_key, "test-key")
        self.assertEqual(gateway.extra_payload, {"thinking": {"type": "disabled"}})


if __name__ == "__main__":
    unittest.main()


class RetryResult(BaseModel):
    value: int


def test_json_retries_do_not_accumulate_failed_code_or_schema():
    for gateway in (OpenAICompatibleGateway("http://test", "test"), OllamaGateway("http://test", "test")):
        requests = []
        original = json.dumps({"requirement": "保留原始要求", "repository_context": {"main.py": "SOURCE"},
                               "output_schema": RetryResult.model_json_schema()}, ensure_ascii=False)
        def respond(request, **kwargs):
            payload = json.loads(request.data)
            requests.append(payload)
            content = json.dumps({"unused": "FAILED_CODE" * 10000}) if len(requests) < 3 else '{"value": 7}'
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}],
                                         "message": {"content": content}}).encode())
        with patch("urllib.request.urlopen", side_effect=respond):
            result = gateway.generate_structured("system", original, RetryResult)
        assert result.value == 7
        assert [len(request["messages"]) for request in requests] == [2, 3, 3]
        for request in requests[1:]:
            assert request["messages"][1]["content"] == original
            assert "FAILED_CODE" not in json.dumps(request["messages"])
            assert '"output_schema"' not in request["messages"][-1]["content"]
        assert abs(len(json.dumps(requests[1])) - len(json.dumps(requests[2]))) < 100


def test_gateway_keeps_latest_validation_error_after_exhaustion():
    gateway = OpenAICompatibleGateway("http://test", "test")
    def respond(*args, **kwargs):
        return io.BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')
    with patch("urllib.request.urlopen", side_effect=respond) as request:
        assert gateway.generate_structured("system", "original", RetryResult) is None
    assert request.call_count == 3
    assert "value" in gateway.last_error
