#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load from local untracked .env if present
if [ -f "${REPO_DIR}/.env" ]; then
    export $(grep -v '^#' "${REPO_DIR}/.env" | xargs)
fi

WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

if [ -z "$WEBHOOK_URL" ]; then
    echo "✖ Error: DISCORD_WEBHOOK_URL is not configured."
    echo "Please define DISCORD_WEBHOOK_URL in ${REPO_DIR}/.env or export it in your shell:"
    echo "  export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'"
    exit 1
fi

VERSION="${1:-v0.1.0-prealpha.5}"
NOTES="${2:-WaveController release update with latest features and improvements.}"

echo "Broadcasting release announcement for ${VERSION} to Discord..."

PAYLOAD=$(jq -n \
  --arg tag "$VERSION" \
  --arg title "🚀 WaveController Release: $VERSION" \
  --arg url "https://github.com/oparada1988/WaveController/releases" \
  --arg desc "$NOTES" \
  '{
    username: "WaveController Releases",
    avatar_url: "https://raw.githubusercontent.com/oparada1988/WaveController/main/assets/icons/WaveController.png",
    embeds: [
      {
        title: $title,
        url: $url,
        description: ($desc + "\n\n**Quick Upgrade Command:**\n```bash\n./install.sh --upgrade\n```"),
        color: 8141549,
        fields: [
          {
            name: "Version Tag",
            value: ("`" + $tag + "`"),
            inline: true
          },
          {
            name: "Repository",
            value: "[GitHub Repository](https://github.com/oparada1988/WaveController)",
            inline: true
          }
        ],
        footer: {
          text: "WaveController • Linux Multi-Track Audio Engine"
        }
      }
    ]
  }')

RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "$PAYLOAD" "$WEBHOOK_URL")

if [ "$RESPONSE" = "204" ] || [ "$RESPONSE" = "200" ]; then
    echo "✔ Announcement broadcasted successfully to Discord (HTTP $RESPONSE)!"
else
    echo "✖ Failed to send announcement (HTTP $RESPONSE). Please check your webhook URL."
    exit 1
fi
