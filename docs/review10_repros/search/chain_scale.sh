#!/bin/bash
# after the proto1/proto2 UI runs: proto cold, then base cold + warm, on the 99M store
R=/tmp/mrfx_review10/search; P=/home/user/Claudevideo/.venv/bin/python; cd $R
until grep -q "^wrote" out/ui_scale_proto2.log 2>/dev/null; do sleep 3; done
repros/serve.sh stop scale
MRFX_SRC=$R/proto repros/serve.sh start scale > out/chain_scale.log
$P repros/ui_search.py http://localhost:8701 scale_proto_cold scale_cfg.json > out/ui_scale_proto_cold.log 2>&1
repros/serve.sh stop scale
repros/serve.sh start scale >> out/chain_scale.log
$P repros/ui_search.py http://localhost:8701 scale_cold2 scale_cfg.json > out/ui_scale_cold2.log 2>&1
$P repros/ui_search.py http://localhost:8701 scale_warm2 scale_cfg.json > out/ui_scale_warm2.log 2>&1
repros/serve.sh stop scale
echo done >> out/chain_scale.log
