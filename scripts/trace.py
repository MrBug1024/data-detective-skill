"""Convenience alias: trace.py [args] is data_run.py trace [args]."""
import sys
from data_run import main

if __name__ == '__main__':
    raise SystemExit(main(['trace', *sys.argv[1:]]))
