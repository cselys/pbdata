"""Mark legacy test_* CLI functions as non-tests without changing their API."""
def cli_entrypoint(function):
    function.__test__ = False
    return function
