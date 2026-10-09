import unittest

from application.consistency import health_result_matches_plan, require_health_result, HealthResultMismatch


class IngressConsistencyTests(unittest.TestCase):
    def test_local_results_require_loopback_http(self):
        for target in ("local-docker", "onprem-compose"):
            plan = {"target": target, "port": 3000, "health_path": "/ready"}
            for url in ("http://127.0.0.1:49152", "http://[::1]:49152"):
                self.assertTrue(health_result_matches_plan(
                    plan, {"url": url, "health_url": url + "/ready"}))
            for url in ("http://example.test:49152", "https://127.0.0.1:49152"):
                self.assertFalse(health_result_matches_plan(
                    plan, {"url": url, "health_url": url + "/ready"}))

    def test_cloud_results_require_external_https(self):
        for target in ("aws-ecs-express", "cloud-run"):
            plan = {"target": target, "port": 3000, "health_path": "/ready"}
            url = "https://service.example.test"
            require_health_result(plan, {"url": url, "health_url": url + "/ready"})
            for url in ("http://service.example.test", "https://localhost:3000",
                        "https://127.0.0.1:3000", "https://[::1]:3000"):
                with self.assertRaisesRegex(HealthResultMismatch, "CV-05"):
                    require_health_result(plan, {"url": url, "health_url": url + "/ready"})

    def test_unknown_target_and_mismatched_probe_are_not_verified(self):
        result = {"url": "https://service.example.test", "health_url": "https://service.example.test/ready"}
        self.assertFalse(health_result_matches_plan(
            {"target": "unknown", "port": 3000, "health_path": "/ready"}, result))
        self.assertFalse(health_result_matches_plan(
            {"target": "cloud-run", "port": 3000, "health_path": "/health"}, result))


if __name__ == "__main__":
    unittest.main()
