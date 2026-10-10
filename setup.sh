#!/bin/bash

echo "Install requirements"

python3 -m venv .venv
source .venv/bin/activate
pip install -r src/requirements.txt
echo
echo "Requirements has installed!"
exit 0