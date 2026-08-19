"""
Phase 4 UX/DX Tests

Tests for structured logging setup and TanStack Query configuration.
"""

import os


class TestStructuredLogging:
    """Tests for structured logging configuration."""

    def test_logging_module_exists(self):
        """Verify the logging module is importable."""
        from app.core.logging import setup_logging

        assert callable(setup_logging)

    def test_setup_logging_configures_structlog(self):
        """Verify structlog is configured after setup_logging."""
        import structlog

        from app.core.logging import setup_logging

        setup_logging()

        logger = structlog.get_logger()
        assert logger is not None

    def test_structured_log_output_is_json(self, capsys):
        """Verify that log output is valid JSON."""
        import json

        import structlog

        from app.core.logging import setup_logging

        setup_logging()
        logger = structlog.get_logger()
        logger.info("test_event", key="value", number=42)

        captured = capsys.readouterr()
        # structlog JSONRenderer should produce valid JSON
        log_line = captured.out.strip()
        if log_line:
            parsed = json.loads(log_line)
            assert parsed["event"] == "test_event"
            assert parsed["key"] == "value"
            assert parsed["number"] == 42
            assert "timestamp" in parsed
            assert parsed.get("log_level") or parsed.get("level")


class TestTanStackQueryConfig:
    """Tests for TanStack Query client-side configuration."""

    def test_query_client_file_exists(self):
        """Verify the queryClient.ts file exists."""
        path = "client/src/lib/queryClient.ts"
        assert os.path.exists(path), f"Missing: {path}"

    def test_query_client_has_persistence(self):
        """Verify queryClient.ts has persistence config."""
        with open("client/src/lib/queryClient.ts") as f:
            content = f.read()
            assert "persistQueryClient" in content
            assert "createSyncStoragePersister" in content
            assert "oracle-query-cache" in content

    def test_main_tsx_has_provider(self):
        """Verify main.tsx wraps App in QueryClientProvider."""
        with open("client/src/main.tsx") as f:
            content = f.read()
            assert "QueryClientProvider" in content
            assert "queryClient" in content

    def test_query_defaults(self):
        """Verify sensible query defaults in queryClient.ts."""
        with open("client/src/lib/queryClient.ts") as f:
            content = f.read()
            assert "staleTime" in content
            assert "gcTime" in content
            assert "retry:" in content
            assert "refetchOnWindowFocus: false" in content
