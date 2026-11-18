#!/bin/bash
# usage: serve.sh start|stop <real|scale>   (only ever kills the PID this script recorded)
R=/tmp/mrfx_review10/search
name=$2
if [ "$1" = stop ]; then
  pid=$(cat $R/out/serve_$name.pid 2>/dev/null)
  [ -n "$pid" ] && kill $pid 2>/dev/null
  for i in $(seq 60); do kill -0 $pid 2>/dev/null || break; sleep 0.5; done
  kill -0 $pid 2>/dev/null && kill -9 $pid
  rm -f $R/out/serve_$name.pid; exit 0
fi
n=$(ls $R/out/serve_${name}_*.log 2>/dev/null | wc -l); n=$((n+1))
log=$R/out/serve_${name}_$n.log
t0=$(date +%s.%N)
[ -n "$MRFX_SRC" ] && export PYTHONPATH=$MRFX_SRC  # A/B: run a scratch copy of the package
nohup /home/user/Claudevideo/.venv/bin/mrfx --config $R/$name.yaml serve > $log 2>&1 &
echo $! > $R/out/serve_$name.pid
for i in $(seq 600); do grep -q "the dashboard is ready" $log && break; sleep 0.2; done
echo "started $name pid $(cat $R/out/serve_$name.pid) in $(echo "$(date +%s.%N) - $t0" | bc) s; log $log; load: $(uptime | sed 's/.*load average: //')"
