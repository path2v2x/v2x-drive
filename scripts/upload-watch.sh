#!/usr/bin/env bash
set -Eeuo pipefail

# Long-duration upload watch for the live perception + twin stack.
#
# Source-controlled port of the previously untracked pr54-watch-24h.sh
# evidence harness, with one behavioral change: operator activity no longer
# fails the watch. The 2026-07-13 24-hour watch failed at phase-4 round 67
# solely because a twin client switched the stream to replay mode while the
# sample ran. This harness classifies a phase-4 sample that shows an active
# Drive session or a twin in replay mode as "occupied", retries it, and
# bounds how much occupancy is tolerable, instead of treating operator
# presence as a system failure. Health, feed, fingerprint, and
# hourly-restart strictness are unchanged from the original.
#
# Environment (defaults in brackets):
#   WATCH_DURATION_SECONDS   total watch time [86400]
#   WATCH_LABEL              evidence directory label [upload-live-watch]
#   TARGET                   required live repo HEAD [current HEAD of LIVE]
#   MAX_OCCUPIED_FRACTION_PCT max % of phase-4 rounds allowed occupied [50]
#   FINAL_GRACE_SECONDS      wait for a clean initial/final phase-4 sample [600]
#
# Self-test: upload-watch.sh --classify <phase4.json> prints
# clean|occupied|fail for an existing verifier output and exits.

LIVE="${LIVE:-/home/path/V2XCarla/v2x-backend}"
WATCH_DURATION_SECONDS="${WATCH_DURATION_SECONDS:-86400}"
WATCH_LABEL="${WATCH_LABEL:-upload-live-watch}"
MAX_OCCUPIED_FRACTION_PCT="${MAX_OCCUPIED_FRACTION_PCT:-50}"
FINAL_GRACE_SECONDS="${FINAL_GRACE_SECONDS:-600}"

PHASE4_OCCUPIED_JQ='
  (.evidence.server_status.active_sessions > 0)
  or (.evidence.twin_status.mode == "replay")
'

PHASE4_STRICT_JQ='
  .ok and .evidence.server_status.active_sessions==0 and
  .evidence.server_status.map.current_map=="richmond" and
  .evidence.twin_status.mode=="live" and
  (.evidence.twin_hello.rig.cameras|sort)==["ch1","ch2","ch3","ch4"] and
  (.evidence.twin_hello.rig.spawn_failures|length)==0 and
  (.evidence.twin_hello.rig.refused_cameras|length)==0 and
  .evidence.twin_status.actors==.evidence.twin_status.tracks and
  (.evidence.twin_status.objects|length)==.evidence.twin_status.tracks and
  ([.evidence.twin_status.objects[].actor_id]|unique|length)==.evidence.twin_status.actors and
  ([.evidence.twin_status.objects[].track_id]|unique|length)==.evidence.twin_status.tracks and
  ([.evidence.twin_status.objects[].object_id]|unique|length)==.evidence.twin_status.tracks and
  ([.evidence.twin_status.objects[] | (
    .actor_present==true and (.actor_id|type)=="number" and
    (.tracked_actor_id|type)=="number" and .actor_id==.tracked_actor_id and
    (.track_id|type)=="number" and (.object_id|type)=="string" and
    (.object_id|length)>0 and .timestamp_schema_version==2 and
    .media_time_trusted==true and
    .detection_timestamp_utc==.media_timestamp_utc and
    .media_clock.source=="hls_ext_x_program_date_time" and
    .media_clock.schema_version==1 and
    .media_clock.evidence_method=="exact_same_session_pts"
  )] | all)
'

classify_phase4_json() {
  # 0 = clean pass, 2 = occupied (operator activity), 1 = fail
  local path=$1
  if jq -e "$PHASE4_OCCUPIED_JQ" "$path" >/dev/null 2>&1; then
    return 2
  fi
  jq -e "$PHASE4_STRICT_JQ" "$path" >/dev/null
}

if [[ "${1:-}" == "--classify" ]]; then
  rc=0
  classify_phase4_json "${2:?usage: upload-watch.sh --classify <phase4.json>}" || rc=$?
  case $rc in
    0) echo clean ;;
    2) echo occupied ;;
    *) echo fail ;;
  esac
  exit 0
fi

TARGET="${TARGET:-$(git -C "$LIVE" rev-parse HEAD)}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
E=/home/path/V2XCarla/v2x-evidence/perception/${STAMP}-${WATCH_LABEL}
END_EPOCH=$(($(date +%s) + WATCH_DURATION_SECONDS))
SAMPLE=0
FEED_ROUND=0
PHASE_ROUND=0
OCCUPIED_ROUND=0
HOURLY_ROUND=0
mkdir -p "$E"
printf '%s\n' "$E" >/tmp/v2x-upload-watch-current-evidence

PERCEPTION_PID=$(systemctl show v2x-perception.service -p MainPID --value)
PERCEPTION_RESTARTS=$(systemctl show v2x-perception.service -p NRestarts --value)
WEB_PID=$(systemctl show v2x-web.service -p MainPID --value)
WEB_RESTARTS=$(systemctl show v2x-web.service -p NRestarts --value)
CARLA_PID=$(systemctl show v2x-carla-rr.service -p MainPID --value)
CARLA_RESTARTS=$(systemctl show v2x-carla-rr.service -p NRestarts --value)
DRIVE_PID=$(systemctl show v2x-drive.service -p MainPID --value)
DRIVE_RESTARTS=$(systemctl show v2x-drive.service -p NRestarts --value)
LAST_TRIGGER=$(systemctl show v2x-hourly-drive-restart.timer -p LastTriggerUSecMonotonic --value)

heartbeat() {
  jq -n \
    --arg status "$1" \
    --arg at "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)" \
    --argjson sample "$SAMPLE" \
    --argjson feeds "$FEED_ROUND" \
    --argjson phase4 "$PHASE_ROUND" \
    --argjson occupied "$OCCUPIED_ROUND" \
    --argjson hourly "$HOURLY_ROUND" \
    --argjson end_epoch "$END_EPOCH" \
    '{status:$status,at:$at,sample:$sample,feed_rounds:$feeds,phase4_clean_rounds:$phase4,phase4_occupied_rounds:$occupied,hourly_restarts:$hourly,end_epoch:$end_epoch}' \
    >"$E/heartbeat.json"
}

fail() {
  local reason=$1
  printf 'UPLOAD_WATCH_FAIL reason=%s sample=%s occupied=%s evidence=%s\n' \
    "$reason" "$SAMPLE" "$OCCUPIED_ROUND" "$E" | tee "$E/result.txt"
  journalctl -u v2x-perception.service -u v2x-drive.service \
    -u v2x-carla-rr.service --since '10 minutes ago' --utc --no-pager \
    >"$E/failure-journal.txt" 2>&1 || true
  heartbeat failed
  exit 1
}

strict_health() {
  local path=$1
  curl -fsS --max-time 5 http://127.0.0.1:8090/health >"$path" 2>/dev/null
  jq -e '
    .status=="ok" and .ready==true and .media_clock_ready==true and
    (.cameras|keys|sort)==["ch1","ch2","ch3","ch4"] and
    ([.cameras[] | (
      .state=="streaming" and .fresh==true and .inference_fresh==true and
      .last_error==null and .reconnect_attempts==0 and
      .media_time_trusted==true and .media_clock_status=="matched" and
      .media_clock_evidence_method=="exact_same_session_pts" and
      .age_seconds<=15 and .inference_age_seconds<=10 and
      .decode_latency_ms>=-1000 and .decode_latency_ms<=10000
    )] | all)
  ' "$path" >/dev/null
}

run_phase4() {
  # 0 = clean pass, 2 = occupied, 1 = fail
  local path=$1
  PYTHONPATH="$LIVE/apps/bridge" \
    /home/path/V2XCarla/carla-venv-310/bin/python \
    "$LIVE/apps/bridge/tools/verify_phase4_live.py" >"$path" 2>"$path.stderr" \
    || return 1
  classify_phase4_json "$path"
}

occupied_budget_ok() {
  local total=$((PHASE_ROUND + OCCUPIED_ROUND))
  # Give the fraction check a floor so one occupied round at the start of a
  # watch cannot trip a 50% budget.
  if (( total < 4 )); then
    return 0
  fi
  (( OCCUPIED_ROUND * 100 <= total * MAX_OCCUPIED_FRACTION_PCT ))
}

# Retries a phase-4 sample through operator occupancy for up to
# FINAL_GRACE_SECONDS; used for the initial and final samples where a single
# clean pass is mandatory.
phase4_with_grace() {
  local path=$1
  local deadline=$(($(date +%s) + FINAL_GRACE_SECONDS))
  local rc
  while :; do
    rc=0
    run_phase4 "$path" || rc=$?
    if [[ $rc -eq 0 ]]; then
      return 0
    fi
    if [[ $rc -eq 2 && "$(date +%s)" -lt $deadline ]]; then
      sleep 15
      continue
    fi
    return 1
  done
}

upload_enabled() {
  sudo grep -qx 'V2X_PERCEPTION_UPLOAD=true' /etc/v2x-perception.env
  local pid
  pid=$(systemctl show v2x-perception.service -p MainPID --value)
  sudo sh -c 'tr "\0" "\n" < /proc/$1/environ' sh "$pid" \
    | grep -qx 'V2X_PERCEPTION_UPLOAD=true'
}

owned_fingerprint() {
  test "$(git -C "$LIVE" rev-parse HEAD)" = "$TARGET"
  test -z "$(git -C "$LIVE" status --porcelain=v1)"
  test "$(systemctl show v2x-perception.service -p MainPID --value)" = "$PERCEPTION_PID"
  test "$(systemctl show v2x-perception.service -p NRestarts --value)" = "$PERCEPTION_RESTARTS"
  test "$(systemctl show v2x-web.service -p MainPID --value)" = "$WEB_PID"
  test "$(systemctl show v2x-web.service -p NRestarts --value)" = "$WEB_RESTARTS"
  upload_enabled
}

wait_hourly_recovery() {
  HOURLY_ROUND=$((HOURLY_ROUND + 1))
  local dir="$E/hourly-$(printf '%02d' "$HOURLY_ROUND")"
  local observed_transition=false
  local old_carla_pid=$CARLA_PID
  local old_drive_pid=$DRIVE_PID
  local rc
  mkdir -p "$dir"
  systemctl show v2x-hourly-drive-restart.service -p ActiveState -p SubState \
    -p ActiveEnterTimestamp -p Result -p ExecMainStatus >"$dir/observed.txt"
  for _ in $(seq 1 300); do
    strict_health "$dir/health-current.json" || fail "health_during_hourly_${HOURLY_ROUND}"
    owned_fingerprint || fail "owned_drift_during_hourly_${HOURLY_ROUND}"
    local state
    state=$(systemctl show v2x-hourly-drive-restart.service -p ActiveState --value)
    test "$state" != failed || fail "hourly_service_failed_${HOURLY_ROUND}"
    if [ "$state" != inactive ] \
      || ! systemctl is-active --quiet v2x-carla-rr.service \
      || ! systemctl is-active --quiet v2x-drive.service \
      || [ "$(systemctl show v2x-carla-rr.service -p MainPID --value)" != "$old_carla_pid" ] \
      || [ "$(systemctl show v2x-drive.service -p MainPID --value)" != "$old_drive_pid" ]; then
      observed_transition=true
    fi
    if [ "$observed_transition" = true ] \
      && [ "$state" = inactive ] \
      && systemctl is-active --quiet v2x-carla-rr.service \
      && systemctl is-active --quiet v2x-drive.service; then
      rc=0
      run_phase4 "$dir/phase4.json" || rc=$?
      # Occupied post-restart samples are retried inside this window rather
      # than failed: an operator reconnecting right after the hourly restart
      # is expected, not a regression.
      if [[ $rc -eq 0 ]]; then
        CARLA_PID=$(systemctl show v2x-carla-rr.service -p MainPID --value)
        CARLA_RESTARTS=$(systemctl show v2x-carla-rr.service -p NRestarts --value)
        DRIVE_PID=$(systemctl show v2x-drive.service -p MainPID --value)
        DRIVE_RESTARTS=$(systemctl show v2x-drive.service -p NRestarts --value)
        systemctl show v2x-hourly-drive-restart.service -p ActiveState -p SubState \
          -p ActiveEnterTimestamp -p InactiveEnterTimestamp -p Result \
          -p ExecMainStatus >"$dir/final.txt"
        printf 'HOURLY_RECOVERY_PASS\n' >"$dir/result.txt"
        heartbeat running
        return 0
      elif [[ $rc -eq 1 ]]; then
        fail "hourly_phase4_${HOURLY_ROUND}"
      fi
    fi
    sleep 1
  done
  fail "hourly_recovery_timeout_${HOURLY_ROUND}"
}

trap 'fail interrupted' INT TERM

owned_fingerprint || fail initial_fingerprint
for unit in v2x-carla-rr.service v2x-drive.service v2x-perception.service \
  v2x-web.service v2x-drive-link-health.timer \
  v2x-perception-link-health.timer v2x-hourly-drive-restart.timer; do
  systemctl is-active --quiet "$unit" || fail "initial_unit_${unit}"
done
strict_health "$E/pre-health.json" || fail initial_health
phase4_with_grace "$E/pre-phase4.json" || fail initial_phase4
systemctl show v2x-carla-rr.service v2x-drive.service v2x-perception.service \
  v2x-web.service -p Id -p MainPID -p NRestarts -p ActiveEnterTimestamp \
  >"$E/pre-services.txt"
heartbeat running

while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
  SAMPLE=$((SAMPLE + 1))
  strict_health "$E/current-health.json" || fail "health_${SAMPLE}"
  owned_fingerprint || fail "fingerprint_${SAMPLE}"
  for timer in v2x-drive-link-health.timer v2x-perception-link-health.timer \
    v2x-hourly-drive-restart.timer; do
    systemctl is-active --quiet "$timer" || fail "timer_${timer}_${SAMPLE}"
  done

  current_trigger=$(systemctl show v2x-hourly-drive-restart.timer \
    -p LastTriggerUSecMonotonic --value)
  if [ "$current_trigger" != "$LAST_TRIGGER" ]; then
    LAST_TRIGGER=$current_trigger
    wait_hourly_recovery
  else
    systemctl is-active --quiet v2x-carla-rr.service || fail "carla_down_${SAMPLE}"
    systemctl is-active --quiet v2x-drive.service || fail "drive_down_${SAMPLE}"
    test "$(systemctl show v2x-carla-rr.service -p MainPID --value)" = "$CARLA_PID" \
      || fail "carla_changed_without_hourly_${SAMPLE}"
    test "$(systemctl show v2x-drive.service -p MainPID --value)" = "$DRIVE_PID" \
      || fail "drive_changed_without_hourly_${SAMPLE}"
  fi

  if [ $((SAMPLE % 12)) -eq 0 ]; then
    FEED_ROUND=$((FEED_ROUND + 1))
    /home/path/V2XCarla/perception-venv/bin/python \
      "$LIVE/apps/perception/tools/verify_live_feeds.py" \
      http://127.0.0.1:8090 \
      >"$E/feed-$(printf '%02d' "$FEED_ROUND").json" \
      2>"$E/feed-$(printf '%02d' "$FEED_ROUND").stderr" \
      || fail "feed_${FEED_ROUND}"
    round_rc=0
    run_phase4 "$E/phase4-$(printf '%03d' "$((PHASE_ROUND + OCCUPIED_ROUND + 1))").json" \
      || round_rc=$?
    if [[ $round_rc -eq 0 ]]; then
      PHASE_ROUND=$((PHASE_ROUND + 1))
    elif [[ $round_rc -eq 2 ]]; then
      OCCUPIED_ROUND=$((OCCUPIED_ROUND + 1))
      occupied_budget_ok || fail "occupied_budget_exceeded"
    else
      fail "phase4_$((PHASE_ROUND + OCCUPIED_ROUND))"
    fi
    jq -c . "$E/current-health.json" >>"$E/health-minute.jsonl"
    heartbeat running
  fi
  sleep 5
done

strict_health "$E/final-health.json" || fail final_health
FEED_ROUND=$((FEED_ROUND + 1))
/home/path/V2XCarla/perception-venv/bin/python \
  "$LIVE/apps/perception/tools/verify_live_feeds.py" \
  http://127.0.0.1:8090 >"$E/final-feed.json" 2>"$E/final-feed.stderr" \
  || fail final_feed
phase4_with_grace "$E/final-phase4.json" || fail final_phase4
PHASE_ROUND=$((PHASE_ROUND + 1))
owned_fingerprint || fail final_fingerprint
printf 'UPLOAD_WATCH_PASS target=%s samples=%s feeds=%s phase4_clean=%s phase4_occupied=%s hourly=%s evidence=%s\n' \
  "$TARGET" "$SAMPLE" "$FEED_ROUND" "$PHASE_ROUND" "$OCCUPIED_ROUND" "$HOURLY_ROUND" "$E" \
  | tee "$E/result.txt"
heartbeat passed
