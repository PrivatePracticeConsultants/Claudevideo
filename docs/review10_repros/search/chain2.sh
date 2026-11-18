#!/bin/bash
# unattended A/B: base vs proto2 on both stores, everything sequential (one server at a time)
R=/tmp/mrfx_review10/search; P=/home/user/Claudevideo/.venv/bin/python; cd $R
L=out/chain2.log
until grep -q "^done" out/chain_scale.log 2>/dev/null; do sleep 3; done
echo "== scale base: abandoned-request demo" >> $L
repros/serve.sh start scale >> $L
$P repros/abandoned.py 8701 100000000 6 > out/abandoned_scale_base.log 2>&1
repros/serve.sh stop scale
for r in r1 r2; do
  repros/serve.sh start scale >> $L
  $P repros/bodies.py 8701 scale_base_$r scale_cfg.json > out/bodies_scale_base_$r.log 2>&1
  repros/serve.sh stop scale
done
MRFX_SRC=$R/proto2 repros/serve.sh start scale >> $L
$P repros/bodies.py 8701 scale_proto2 scale_cfg.json > out/bodies_scale_proto2.log 2>&1
repros/serve.sh stop scale
MRFX_SRC=$R/proto2 repros/serve.sh start scale >> $L
$P repros/ui_search.py http://localhost:8701 scale_p2_cold scale_cfg.json > out/ui_scale_p2_cold.log 2>&1
$P repros/ui_search.py http://localhost:8701 scale_p2_warm scale_cfg.json > out/ui_scale_p2_warm.log 2>&1
$P repros/abandoned.py 8701 100000000 6 > out/abandoned_scale_proto2_noheader.log 2>&1
repros/serve.sh stop scale
echo "== real" >> $L
for r in r1 r2; do
  repros/serve.sh start real >> $L
  $P repros/bodies.py 8702 real_base_$r real_cfg.json > out/bodies_real_base_$r.log 2>&1
  if [ $r = r2 ]; then
    for u in "grain=entity&dollar_only=1" "grain=entity&dollar_only=1&q=1013304435" "grain=entity&dollar_only=1&q=263531132" "grain=entity&dollar_only=1&q=dakota+phys" "grain=npi&dollar_only=1&q=1013304435" "grain=npi&dollar_only=1&q=dakota+phys" "grain=npi&dollar_only=1"; do
      for i in 1 2; do printf "%-70s %s  load %s\n" "rates?$u" "$(curl -s -o /dev/null -w '%{time_total}s %{http_code} %{size_download}b' "http://localhost:8702/api/rates?$u&sort=negotiated_rate&dir=desc&page=1&page_size=100")" "$(uptime | sed 's/.*load average: //')"; done
    done > out/real_entity_npi_grain.log 2>&1
  fi
  repros/serve.sh stop real
done
MRFX_SRC=$R/proto2 repros/serve.sh start real >> $L
$P repros/bodies.py 8702 real_proto2 real_cfg.json > out/bodies_real_proto2.log 2>&1
repros/serve.sh stop real
MRFX_SRC=$R/proto2 repros/serve.sh start real >> $L
$P repros/ui_search.py http://localhost:8702 real_p2_cold real_cfg.json > out/ui_real_p2_cold.log 2>&1
$P repros/ui_search.py http://localhost:8702 real_p2_warm real_cfg.json > out/ui_real_p2_warm.log 2>&1
repros/serve.sh stop real
echo done >> $L
