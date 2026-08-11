# Keep pytest from collecting the suite modules directly.
#
# test_correctness.py and test_hostile_inputs.py are standalone scripts. Their `test_*`
# functions report outcomes through record(), which appends to a list and prints -- it
# never raises. Pass/fail is carried by main()'s exit code and by the JSON written to
# results/. If pytest collected those functions it would run them, watch them record
# FAIL, see no exception, and report PASSED. A CI job built on that would be green while
# the security suite was failing, which is worse than having no CI at all.
#
# test_suites.py is the supported pytest entry point: it runs each suite as a subprocess
# and asserts the exit code.
collect_ignore = [
    "test_correctness.py",
    "test_hostile_inputs.py",
    "_fuzz_child.py",
]
