"""Pluggable ticket and notifier backends.

Every backend implements one of the two ABCs and is registered in the
matching manager. Adding a platform means adding a file and a registry
entry -- callers never learn which platforms exist.
"""
