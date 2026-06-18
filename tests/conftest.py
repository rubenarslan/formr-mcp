def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: live tests that create/delete real runs on a configured "
        "formr instance (skipped unless credentials are present in .env)",
    )
