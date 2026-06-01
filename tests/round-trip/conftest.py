"""pytest config for round-trip suite — registers the --inject CLI flag."""


def pytest_addoption(parser):
    parser.addoption(
        "--inject",
        action="store_true",
        default=False,
        help="Push a fresh queue item and wait for the next loop tick.",
    )
