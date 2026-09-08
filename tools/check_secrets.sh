#!/bin/bash
# Scanner: looks for credentials and client data before publishing.
# Usage:  bash tools/check_secrets.sh      (from the repo root)
# Exits with code 1 if it finds anything suspicious.
cd "$(dirname "$0")/.." || exit 1
FINDINGS=0

scan() {
  local title="$1" patron="$2"
  local r
  r=$(grep -rnIE "$patron" . \
        --exclude-dir=.git --exclude-dir=node_modules \
        --exclude=check_secrets.sh --exclude=.env.example 2>/dev/null | head -8)
  if [ -n "$r" ]; then
    echo ""
    echo "  [!] $title"
    echo "$r" | sed 's/^/      /'
    FINDINGS=$((FINDINGS+1))
  fi
}

echo "Scanning for credentials and client data..."

scan "API keys (OpenAI, GitHub, JWT, PIT)" \
        '\b(sk-[A-Za-z0-9_-]{20,}|gh[pous]_[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_.-]{30,}|pit-[A-Za-z0-9-]{20,})\b'
scan "Secret assignment with a real value" \
        '(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)[[:space:]]*=[[:space:]]*["'"'"']?[A-Za-z0-9_/+.-]{16,}'
scan "Public IP addresses" \
        '\b(?!10\.|127\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|203\.0\.113\.|0\.)([0-9]{1,3}\.){3}[0-9]{1,3}\b'
scan "Real phone numbers (outside the 555 example range)" \
        '\+?1[2-9][0-9]{2}(?!555)[0-9]{7}\b'
scan "Credentials in Python dicts or parameters" \
        "(password|passwd|secret|api_key|access_key|secret_key)[[:space:]]*=[[:space:]]*[\"'][^\"'<]{8,}[\"']"
scan "Real email addresses" \
        '[A-Za-z0-9._%+-]+@(?!example\.(com|org))[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
scan "Own domains" \
        'https?://(?!example\.com|services\.leadconnectorhq\.com|api\.openai\.com|api\.telnyx\.com|127\.0\.0\.1|localhost)[a-z0-9.-]+\.[a-z]{2,}'

echo ""
if [ "$FINDINGS" -eq 0 ]; then
  echo "  OK - no credentials or client data found."
  exit 0
fi
echo "  $FINDINGS categorie(s) with findings. Review and clean BEFORE publishing."
echo "  (If it is a false positive, add the exception in this script.)"
exit 1
