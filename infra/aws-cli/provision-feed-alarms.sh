#!/usr/bin/env bash
set -euo pipefail

# Creates an SNS alert topic plus one CloudWatch alarm per Kinesis Video
# Stream that fires when the stream stops receiving fragments. Missing data
# is treated as breaching, so producer silence is itself the alarm signal —
# this is the AWS-side guard that catches a field-site producer outage even
# if the Path PC is also down.
#
# Run with a principal allowed to manage SNS topics and CloudWatch alarms.
# The steady-state rfs-v2x-service user is NOT authorized for these calls;
# use the same separately authorized principal that runs the other
# provision scripts.
#
# Usage:
#   AWS_REGION=us-west-2 ALERT_EMAIL=ops@example.com ./provision-feed-alarms.sh

AWS_REGION="${AWS_REGION:-us-west-2}"
TOPIC_NAME="${TOPIC_NAME:-v2x-feed-alerts}"
ALERT_EMAIL="${ALERT_EMAIL:-}"
STREAMS="${STREAMS:-v2x-backend-cam-ch1,v2x-backend-cam-ch2,v2x-backend-cam-ch3,v2x-backend-cam-ch4}"
# 3 consecutive 5-minute periods with under one fragment => alarm within
# ~15 minutes of the producer stopping.
PERIOD_SECONDS="${PERIOD_SECONDS:-300}"
EVALUATION_PERIODS="${EVALUATION_PERIODS:-3}"

topic_arn="$(aws sns create-topic \
  --region "$AWS_REGION" \
  --name "$TOPIC_NAME" \
  --query TopicArn --output text)"
echo "topic: $topic_arn"

if [[ -n "$ALERT_EMAIL" ]]; then
  aws sns subscribe \
    --region "$AWS_REGION" \
    --topic-arn "$topic_arn" \
    --protocol email \
    --notification-endpoint "$ALERT_EMAIL" >/dev/null
  echo "email subscription requested for $ALERT_EMAIL (confirm via inbox)"
fi

IFS=',' read -r -a stream_list <<<"$STREAMS"
for stream in "${stream_list[@]}"; do
  alarm_name="v2x-feed-stalled-${stream}"
  aws cloudwatch put-metric-alarm \
    --region "$AWS_REGION" \
    --alarm-name "$alarm_name" \
    --alarm-description "No fragments arriving on KVS stream ${stream}" \
    --namespace AWS/KinesisVideo \
    --metric-name PutMedia.IncomingFragments \
    --dimensions "Name=StreamName,Value=${stream}" \
    --statistic Sum \
    --period "$PERIOD_SECONDS" \
    --evaluation-periods "$EVALUATION_PERIODS" \
    --threshold 1 \
    --comparison-operator LessThanThreshold \
    --treat-missing-data breaching \
    --alarm-actions "$topic_arn" \
    --ok-actions "$topic_arn"
  echo "alarm: $alarm_name"
done

echo "done. Alarms fire after $((PERIOD_SECONDS * EVALUATION_PERIODS / 60)) minutes of producer silence."
