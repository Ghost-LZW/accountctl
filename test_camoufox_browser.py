import os
import unittest
from unittest.mock import patch

from camoufox.ip import Proxy

from camoufox_browser import build_proxy_config


class ProxyConfigTests(unittest.TestCase):
    def test_build_proxy_config_is_accepted_by_camoufox_geoip_proxy(self):
        with patch.dict(os.environ, {}, clear=True):
            proxy_config, no_proxy = build_proxy_config("http://user:pass@127.0.0.1:8080")

        self.assertEqual(
            proxy_config,
            {
                "server": "http://127.0.0.1:8080",
                "username": "user",
                "password": "pass",
            },
        )
        self.assertNotIn("bypass", proxy_config)
        self.assertIn("127.0.0.1", no_proxy)

        Proxy(**proxy_config)


if __name__ == "__main__":
    unittest.main()
