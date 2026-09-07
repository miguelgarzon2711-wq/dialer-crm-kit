#!/bin/bash
# flock evita corridas concurrentes (con -n sale en silencio si ya hay otra corriendo)
exec flock -n /tmp/transcribe_calls.lock python3 /root/transcribe_calls.py
