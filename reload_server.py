#!/usr/bin/env python3
"""
Reload the PythonAnywhere web app from a Bash console.

Usage:
    python reload_server.py

Requires $API_TOKEN, which PythonAnywhere auto-populates in every console
AFTER you create a token once:
  1. Go to: https://www.pythonanywhere.com/account/#api_token
  2. Click "Create a new API token"
  3. Open a fresh Bash console — $API_TOKEN will be set automatically.
"""

import os
import sys
import time
import requests

USERNAME = os.path.basename(os.path.expanduser('~')).lower()
DOMAIN = f'{USERNAME}.pythonanywhere.com'
RELOAD_URL = f'https://www.pythonanywhere.com/api/v0/user/{USERNAME}/webapps/{DOMAIN}/reload/'
CHECK_URL = f'https://{DOMAIN}/api/status'


def main():
    api_token = os.environ.get('API_TOKEN', '').strip()
    if not api_token:
        print('ERROR: $API_TOKEN is not set.')
        print()
        print('To fix this (one-time setup):')
        print('  1. Go to https://www.pythonanywhere.com/account/#api_token')
        print('  2. Click "Create a new API token"')
        print('  3. Open a fresh PythonAnywhere Bash console')
        print('  4. $API_TOKEN will be pre-populated — run this script again.')
        return 1

    print(f'Reloading {DOMAIN} ...', flush=True)
    resp = requests.post(RELOAD_URL, headers={'Authorization': f'Token {api_token}'})
    if resp.status_code != 200:
        print(f'ERROR: reload returned HTTP {resp.status_code}: {resp.text}')
        return 1
    print(f'  Reload accepted.')

    print('  Waiting for server', end='', flush=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(2)
        print('.', end='', flush=True)
        try:
            r = requests.get(CHECK_URL, timeout=5)
            if r.status_code in (200, 401, 403):
                print(f' up (HTTP {r.status_code})')
                print('Server reloaded successfully.')
                return 0
        except requests.exceptions.RequestException:
            pass

    print(' timed out')
    print('WARNING: Could not confirm server came back up.')
    print('Check the PythonAnywhere Web tab for error logs.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
