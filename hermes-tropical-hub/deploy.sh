#!/bin/bash
set -e

APP="hermes-tropical-hub"
REGION="gru"

echo "=== Hermes Agent — Deploy no Fly.io ==="
echo ""

# 1. Login
echo "[ 1/5 ] Login no Fly.io..."
fly auth login
echo ""

# 2. Criar o app
echo "[ 2/5 ] Criando app '$APP'..."
fly apps create "$APP" --org personal 2>/dev/null || echo "App já existe, continuando..."
echo ""

# 3. Criar volume persistente
echo "[ 3/5 ] Criando volume persistente 'hermes_data' em $REGION..."
fly volumes create hermes_data --size 1 --region "$REGION" -a "$APP" 2>/dev/null || echo "Volume já existe, continuando..."
echo ""

# 4. Configurar secrets
# IMPORTANTE: Hermes espera SLACK_BOT_TOKEN e SLACK_APP_TOKEN
# Se seus tokens estão salvos como TROPICAL_BOT_TOKEN / TROPICAL_APP_TOKEN, use os mesmos valores
echo "[ 4/5 ] Configure os secrets abaixo e pressione ENTER para continuar:"
echo ""
echo "  fly secrets set -a $APP \\"
echo "    TROPICAL_BOT_TOKEN=xoxb-...    # token do bot (começa com xoxb-)"
echo "    TROPICAL_APP_TOKEN=xapp-...    # token socket mode (começa com xapp-)"
echo "    OPENAI_API_KEY=sk-...          # chave OpenAI"
echo "    HUBSPOT_TOKEN=pat-...          # token HubSpot"
echo ""
echo "  (Valores estão em: fly secrets list -a tropical-bot-v2)"
echo ""
read -p "Pressione ENTER após configurar os secrets..."

# 5. Deploy
echo "[ 5/5 ] Fazendo deploy..."
fly deploy -a "$APP"
echo ""
echo "=== Deploy concluído! ==="
echo ""
echo "Logs ao vivo:  fly logs -a $APP"
echo "Status:        fly status -a $APP"
echo "SSH na máquina: fly ssh console -a $APP"
