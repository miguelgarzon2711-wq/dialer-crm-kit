#!/bin/bash
# Verificador: busca credenciales y datos de clientes antes de publicar.
# Uso:  bash tools/check_secrets.sh      (desde la raíz del repo)
# Sale con código 1 si encuentra algo sospechoso.
cd "$(dirname "$0")/.." || exit 1
HALLAZGOS=0

revisar() {
  local titulo="$1" patron="$2"
  local r
  r=$(grep -rnIE "$patron" . \
        --exclude-dir=.git --exclude-dir=node_modules \
        --exclude=check_secrets.sh --exclude=.env.example 2>/dev/null | head -8)
  if [ -n "$r" ]; then
    echo ""
    echo "  [!] $titulo"
    echo "$r" | sed 's/^/      /'
    HALLAZGOS=$((HALLAZGOS+1))
  fi
}

echo "Buscando credenciales y datos de clientes..."

revisar "Claves de API (OpenAI, GitHub, JWT, PIT)" \
        '\b(sk-[A-Za-z0-9_-]{20,}|gh[pous]_[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_.-]{30,}|pit-[A-Za-z0-9-]{20,})\b'
revisar "Asignación de secreto con valor real" \
        '(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)[[:space:]]*=[[:space:]]*["'"'"']?[A-Za-z0-9_/+.-]{16,}'
revisar "Direcciones IP públicas" \
        '\b(?!10\.|127\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|203\.0\.113\.|0\.)([0-9]{1,3}\.){3}[0-9]{1,3}\b'
revisar "Teléfonos reales (no del rango 555 de ejemplo)" \
        '\+?1[2-9][0-9]{2}(?!555)[0-9]{7}\b'
revisar "Credenciales en diccionarios o parámetros de Python" \
        "(password|passwd|secret|api_key|access_key|secret_key)[[:space:]]*=[[:space:]]*[\"'][^\"'<]{8,}[\"']"
revisar "Correos electrónicos reales" \
        '[A-Za-z0-9._%+-]+@(?!example\.(com|org))[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
revisar "Dominios propios" \
        'https?://(?!example\.com|services\.leadconnectorhq\.com|api\.openai\.com|api\.telnyx\.com|127\.0\.0\.1|localhost)[a-z0-9.-]+\.[a-z]{2,}'

echo ""
if [ "$HALLAZGOS" -eq 0 ]; then
  echo "  OK — no se encontraron credenciales ni datos de clientes."
  exit 0
fi
echo "  $HALLAZGOS categoría(s) con hallazgos. Revisá y limpiá ANTES de publicar."
echo "  (Si es un falso positivo, agregá la excepción en este script.)"
exit 1
