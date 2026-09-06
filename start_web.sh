#!/usr/bin/env bash
# 论文查重网页版一键启动(Mac / Linux)
cd "$(dirname "$0")"
python3 scripts/webapp.py --port 8765
