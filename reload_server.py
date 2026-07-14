#!/usr/bin/env python3
"""
Reload the PythonAnywhere web app from the console and confirm it came back up.

Usage (from a PythonAnywhere Bash console):
    python reload_server.py

Requires the $API_TOKEN environment variable, which PythonAnywhere pre-populates
in all consoles. No arguments needed.

Update DOMAIN below to match the PythonAnywhere domain for this app.
"""

import os
import sys
import time
import requests

USERNAME = 'jmfranck'
DOMAIN   = 'jmfranck.pythonanywhere.com'   # update if this app uses a different domain
RELOAD_URL = f'https://www.pythonanywhere.com/api/v0/user/{USERNAME}/webapps/{DOMAIN}/reload/'
CHECK_URL  = f'https://{DOMAIN}/debug/health'


def main():
    api_token = os.environ.get('API_TOKEN', '').strip()
    if not api_token:
        print('ERROR: $API_TOKEN is not set.')
        print('Run this script from a PythonAnywhere Bash console — the token is pre-populated there.')
        return 1

    print(f'Reloading {DOMAIN} ...', flush=True)
    resp = requests.post(RELOAD_URL, headers={'Authorization': f'Token {api_token}'})

    if resp.status_code == 200:
        print(f'  Reload accepted (HTTP {resp.status_code})')
    else:
        print(f'  ERROR: reload returned HTTP {resp.status_code}')
        print(f'  Body: {resp.text}')
        return 1

    # Poll the site until it responds, confirming the worker restarted
    print('  Waiting for server to come back up', end='', flush=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(2)
        print('.', end='', flush=True)
        try:
            check = requests.get(CHECK_URL, timeout=5)
            if check.status_code in (200, 302, 401, 403):
                # Any of these means the Flask app is running
                print(f' up (HTTP {check.status_code})')
                print('Server reloaded successfully.')
                return 0
        except requests.exceptions.RequestException:
            pass  # still starting up

    print(' timed out')
    print('WARNING: Could not confirm the server came back up within 30 seconds.')
    print('Check the PythonAnywhere web tab for error logs.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
