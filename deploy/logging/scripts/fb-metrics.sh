#!/usr/bin/env bash
# Fluent Bit の監視 endpoint をホストから読む（Fluent Bit は外への経路の無い internal network にだけいる）。
#   deploy/logging/scripts/fb-metrics.sh [--project avp2-logging] [path]
#   path の既定 /api/v1/metrics/prometheus。他: /api/v1/storage /api/v2/health /api/v2/metrics/prometheus
set -euo pipefail
PROJECT="avp2-logging"
if [[ "${1:-}" == "--project" ]]; then PROJECT="$2"; shift 2; fi
path="${1:-/api/v1/metrics/prometheus}"
img="opensearchproject/opensearch:3.8.0@sha256:fafe3fc3587088674669235575aa166228c48bdb940294a8cdbbc1da75236a40"
exec docker run --rm --network "${PROJECT}_internal" --entrypoint curl "$img" -s "http://fluent-bit:2020$path"
