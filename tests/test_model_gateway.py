import os
import unittest
from unittest.mock import patch

from dev_agent.llm.gateway import OpenAICompatibleGateway, build_model_gateway


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
