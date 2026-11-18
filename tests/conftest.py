def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: live tests that create/delete real runs on a configured "
        "formr instance (only with `-m integration`, and only with credentials in .env)",
    )
