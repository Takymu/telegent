#!/data/data/com.termux/files/usr/bin/bash
# Publishes the tunnel's current address to url.txt on the gh-pages branch, so tg.py and the permanent
# page https://takymu.github.io/telegent/ find the server after a restart. Called by start.sh.
# Needs a deploy key with write access: ~/.ssh/telegent_deploy + "Host github-telegent" in ~/.ssh/config.
# Retries for about half an hour (the network may come up later than the tunnel).
URL="$1"
[ -n "$URL" ] || exit 1
DIR=~/telegent-pages
LOG=~/telegent-logs/publish.log
export GIT_TERMINAL_PROMPT=0

for i in $(seq 1 30); do
  # a newer address appeared meanwhile: let its own publisher do the job
  [ "$(cat ~/telegent-url.txt 2>/dev/null)" = "$URL" ] || exit 0
  {
    if [ ! -d "$DIR/.git" ]; then
      rm -rf "$DIR" && git clone -q --depth 1 -b gh-pages git@github-telegent:Takymu/telegent.git "$DIR"
    fi &&
    cd "$DIR" && git fetch -q --depth 1 origin gh-pages && git reset -q --hard origin/gh-pages &&
    if [ "$(cat url.txt 2>/dev/null)" = "$URL" ]; then
      echo "$(date '+%F %T') already published $URL"; exit 0
    fi &&
    echo "$URL" > url.txt &&
    git -c user.name=Takymu -c user.email=141811344+Takymu@users.noreply.github.com commit -q -m "url: $URL" url.txt &&
    git push -q origin HEAD:gh-pages &&
    { echo "$(date '+%F %T') published $URL"; exit 0; }
  } >> "$LOG" 2>&1
  sleep 60
done
echo "$(date '+%F %T') FAILED to publish $URL" >> "$LOG"
exit 1
