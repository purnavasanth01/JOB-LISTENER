#!/bin/bash
# Double-click when the Mac says "Overleaf login needed": log in once (solve the image check), window closes itself.
cd "$(dirname "$0")" && ./.venv/bin/python overleaf.py --login
