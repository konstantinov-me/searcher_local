#!/usr/bin/env bash
# Самоподписанный сертификат для внутренней сети (если нет DNS/Let's Encrypt).
# Клиенты в config.yaml: server.verify_tls: /путь/к/ca.crt  (или localsearch CAfile)
set -euo pipefail
DIR="${1:-/etc/nginx/tls}"; CN="${2:-search.example.local}"; DAYS="${3:-825}"
mkdir -p "$DIR"

# 1) свой мини-CA (его раздаём клиентам для verify_tls)
openssl req -x509 -newkey rsa:2048 -sha256 -days "$DAYS" -nodes \
  -keyout "$DIR/ca.key" -out "$DIR/ca.crt" \
  -subj "/CN=LocalSearch Internal CA"

# 2) серверный сертификат с SAN на CN и localhost
openssl req -newkey rsa:2048 -nodes -keyout "$DIR/$CN.key" -out "$DIR/server.csr" \
  -subj "/CN=$CN"
printf "subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1\n" "$CN" > "$DIR/san.cnf"
openssl x509 -req -in "$DIR/server.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" \
  -CAcreateserial -days "$DAYS" -sha256 -extfile "$DIR/san.cnf" -out "$DIR/$CN.crt"

echo "готово: $DIR/ca.crt (клиентам), $DIR/$CN.crt + $DIR/$CN.key (nginx/uvicorn)"
