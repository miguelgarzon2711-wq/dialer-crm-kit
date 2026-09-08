#!/bin/bash
# flock prevents concurrent runs (-n exits silently if another one is already running)
exec flock -n /tmp/transcribe_calls.lock python3 /root/transcribe_calls.py
