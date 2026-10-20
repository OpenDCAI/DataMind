"""Run a required test slice; missing services/dependencies must fail CI."""
import sys

import pytest


class RequireExecutedTests:
    def pytest_sessionfinish(self, session, exitstatus):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        skipped = reporter.stats.get("skipped", []) if reporter else []
        if skipped:
            if reporter:
                reporter.write_sep("!", f"Required test slice skipped {len(skipped)} tests")
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


if __name__ == "__main__":
    raise SystemExit(pytest.main(sys.argv[1:], plugins=[RequireExecutedTests()]))
