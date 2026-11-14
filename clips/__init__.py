"""Clips: a separate system that cuts short videos from sources we have the right
to use, lets a person approve them, and posts them to its own YouTube channel.

It runs as its own process with its own data folder, its own Google sign-in and
its own settings. It only adds files to this repo and never changes what the
main app does. Start with `python -m clips status`.
"""
