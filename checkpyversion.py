import sys
def check_py_version():
    if sys.version_info <= (3, 0):
        sys.stdout.write("Sorry, requires Python 3.x, not Python 2.x\n")
        sys.exit(1)