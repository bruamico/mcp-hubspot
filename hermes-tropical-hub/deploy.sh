#!/bin/bash
set -e

APP="hermes-tropical-hub"
REGION="gru"
SOURCE_APP="tropical-bot-v2"  # where existing secrets live

echo "=== Hermes Agent — Deploy no Fly.io ==="
echo ""

# 1. Login
echo "[ 1/5 ] Login no Fly.io..."
fly auth login
echo ""

# 2. Criar o app (ignora erro se já existir)
echo "[ 2/5 ] Criando app '$APP'..."
fly apps create "$APP" --org personal 2>/dev/null || echo "App já existe, continuando..."
echo ""

# 3. Criar volume persistente (ignora erro se já existir)
echo "[ 3/5 ] Criando volume persistente 'hermes_data' em $REGION..."
fly volumes create hermes_data --size 1 --region "$REGION" -a "$APP" 2>/dev/null || echo "Volume já existe, continuando..."
echo ""

# 4. Migrar secrets do app de origem
echo "[ 4/5 ] Copiando secrets de '$SOURCE_APP' para '$APP'..."
echo ""
echo "  Buscando TROPICAL_BOT_TOKEN..."
BOT_TOKEN=$(fly secrets list -a "$SOURCE_APP" --json 2>/dev/null | python3 -c "
import sys, json
secrets = json.load(sys.stdin)
for s in secrets:
    if s.get('Name') == 'TROPICAL_BOT_TOKEN':
        print(s.get('Digest', ''))
" 2>/dev/null || echo "")

echo ""
echo "  ATENÇÃO: Os secrets precisam ser definidos manualmente."
echo "  Execute o comando abaixo substituindo pelos valores reais:"
echo ""
echo "  fly secrets set -a $APP \\"
echo "    TROPICAL_BOT_TOKEN=xoxb-... \\"
echo "    TROPICAL_APP_TOKEN=xapp-... \\"
echo "    OPENAI_API_KEY=sk-... \\"
echo "    HUBSPOT_TOKEN=pat-..."
echo ""
echo "  (Busque os valores em: fly secrets list -a $SOURCE_APP)"
echo ""
read -p "Pressione ENTER após configurar os secrets para continuar com o deploy..."

# 5. Deploy
echo "[ 5/5 ] Fazendo deploy..."
fly deploy -a "$APP"
echo ""
echo "=== Deploy concluído! ==="
echo "Logs em tempo real: fly logs -a $APP"
echo "Status:             fly status -a $APP"
