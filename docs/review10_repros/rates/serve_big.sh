#!/bin/bash
# start the 32M copy on port 8710 (cwd deliberately NOT the repo)
cd /tmp/mrfx_review10/rates/big
nohup /home/user/Claudevideo/.venv/bin/mrfx --config /tmp/mrfx_review10/rates/big/mrfx.yaml serve > serve.log 2>&1 &
echo $! > serve.pid
for i in $(seq 1 60); do grep -q "dashboard is ready" serve.log && break; sleep 1; done
tail -1 serve.log
