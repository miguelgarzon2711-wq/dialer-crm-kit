#!/bin/bash
if ! curl -s --max-time 3 'http://10.22.22.1:8055/pick?tel=123' >/dev/null 2>&1; then
  systemctl restart did-picker
  echo "$(date): did-picker was not responding — restarted" >> /var/log/did_picker_watchdog.log
fi
