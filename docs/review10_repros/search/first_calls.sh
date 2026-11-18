#!/bin/bash
# usage: first_calls.sh <real|scale> <port> <evict:0|1>  — restart the server and time the page-load calls in the order the page makes them
R=/tmp/mrfx_review10/search; name=$1; port=$2
$R/repros/serve.sh stop $name
if [ "$3" = 1 ]; then python3 $R/repros/evict.py $(grep store_dir $R/$name.yaml | awk '{print $2}'); fi
$R/repros/serve.sh start $name
for u in "/api/catalog" "/api/stats" "/api/states" "/api/payers" "/api/months" "/api/rates?grain=tin&dollar_only=1&sort=negotiated_rate&dir=desc&page=1&page_size=100" "/api/summary?grain=tin&dollar_only=1" "/api/stats" "/api/rates?grain=tin&dollar_only=1&sort=negotiated_rate&dir=desc&page=1&page_size=100" "/api/summary?grain=tin&dollar_only=1"; do
  printf "%-100s %s   load %s\n" "$u" "$(curl -s -o /dev/null -w '%{time_total}s %{http_code}' "http://localhost:$port$u")" "$(uptime | sed 's/.*load average: //')"
done
