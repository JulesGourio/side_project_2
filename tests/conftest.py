"""Test environment: the suite runs as a development app, with no visitor header.

The access checks treat ENV=development as the only open mode, so the tests set it explicitly
instead of relying on the default, which is production.
"""
import os

os.environ.setdefault('ENV', 'development')
