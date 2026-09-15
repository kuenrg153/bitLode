#!/bin/bash
# Run a shell command on the Whatsminer control board over SSH.
# One entry point, so it can be allowed as a single agent permission rule and every
# miner-side write goes through one auditable file.
#
# Requires the whatsminer_private_api_py package (wm_ssh.py). Point WM_API_DIR at it.
#   WM_API_DIR  directory containing wm_ssh.py   (default: ./vendor/whatsminer_private_api_py-main)
#   PYTHON      interpreter with paramiko         (default: python3)
HERE="$(cd "$(dirname "$0")" && pwd)"
WM_API_DIR="${WM_API_DIR:-$HERE/../vendor/whatsminer_private_api_py-main}"
exec "${PYTHON:-python3}" "$WM_API_DIR/wm_ssh.py" "$@"
